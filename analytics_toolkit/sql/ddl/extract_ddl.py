from __future__ import annotations

from collections.abc import Sequence
import re
from typing import Any, cast

import pandas as pd

from ..backend_adapters import (
    UNSUPPORTED_BACKEND_MESSAGE,
    split_trino_table_name,
)
from ..connection.config import get_connection_config
from ..connection.errors import InvalidSqlInputError, UnsupportedConnectionTypeError
from ..dml.io.read_sql import read_sql
from .identifiers import quote_identifier
from ..execution.operation_runner import timed_public_sql_function


_GREENPLUM_BACKENDS = frozenset({"gp"})


@timed_public_sql_function
def extract_ddl(db_key: str, tables: str | Sequence[str]) -> str:
    """Return semicolon-terminated table DDL statements joined by newlines."""

    config = get_connection_config(db_key)
    table_names = _validate_tables(tables)
    ddl_statements = [
        _normalize_ddl(
            _extract_table_ddl(
                config.connection_key,
                config.backend,
                table_name,
            ),
            table_name,
        )
        for table_name in table_names
    ]
    return "\n".join(ddl_statements)


def _extract_table_ddl(connection_key: str, backend: str, table_name: str) -> str:
    if backend in _GREENPLUM_BACKENDS:
        return _extract_greenplum_table_ddl(connection_key, table_name)

    query = _build_extract_ddl_query(connection_key, backend, table_name)
    result = cast(pd.DataFrame, read_sql(connection_key, query))
    return _first_result_value(result, table_name)


def _build_extract_ddl_query(
    connection_key: str,
    backend: str,
    table_name: str,
) -> str:
    if backend == "ch":
        return f"SHOW CREATE TABLE {table_name}"
    if backend == "trino":
        catalog, schema_name, relation_name = split_trino_table_name(
            table_name,
            connection_key=connection_key,
        )
        return f"SHOW CREATE TABLE {catalog}.{schema_name}.{relation_name}"
    if backend == "gp":
        return (
            "SELECT "
            f"pg_catalog.pg_get_tabledef(pg_catalog.to_regclass({_sql_string_literal(table_name)})::oid) "
            "AS ddl"
        )

    raise UnsupportedConnectionTypeError(UNSUPPORTED_BACKEND_MESSAGE)


def _extract_greenplum_table_ddl(connection_key: str, table_name: str) -> str:
    query = _build_extract_ddl_query(connection_key, "gp", table_name)
    try:
        result = cast(pd.DataFrame, read_sql(connection_key, query))
    except Exception as exc:
        if not _is_missing_pg_get_tabledef_error(exc):
            raise
        return _extract_greenplum_catalog_ddl(connection_key, table_name)
    return _first_result_value(result, table_name)


def _extract_greenplum_catalog_ddl(connection_key: str, table_name: str) -> str:
    relation = _read_single_row(
        connection_key,
        _build_gp_relation_query(table_name),
        table_name,
    )
    oid = _require_metadata_value(relation, "oid", table_name)
    schema_name = _require_metadata_value(relation, "schema_name", table_name)
    relation_name = _require_metadata_value(relation, "relation_name", table_name)
    reloptions = _optional_metadata_value(relation, "reloptions")
    table_comment = _optional_metadata_value(relation, "table_comment")

    columns = cast(
        pd.DataFrame,
        read_sql(connection_key, _build_gp_columns_query(str(oid))),
    )
    if columns.empty:
        raise ValueError(f"No columns returned for table {table_name}.")

    constraints = cast(
        pd.DataFrame,
        read_sql(connection_key, _build_gp_constraints_query(str(oid))),
    )
    parents = cast(
        pd.DataFrame,
        read_sql(connection_key, _build_gp_inherits_query(str(oid))),
    )
    indexes = cast(
        pd.DataFrame,
        read_sql(connection_key, _build_gp_indexes_query(str(oid))),
    )

    qualified_name = _quote_gp_table_name(schema_name, relation_name)
    column_lines = [_format_gp_column_definition(row) for row in _rows(columns)]
    constraint_lines = [
        _format_gp_constraint_definition(row)
        for row in _rows(constraints)
        if _optional_metadata_value(row, "constraint_def")
    ]
    create_sql = "CREATE TABLE " + qualified_name + " (\n    "
    create_sql += ",\n    ".join([*column_lines, *constraint_lines])
    create_sql += "\n)"

    inherits_sql = _format_gp_inherits_clause(parents)
    if inherits_sql:
        create_sql += f"\n{inherits_sql}"

    storage_sql = _format_gp_storage_clause(reloptions)
    if storage_sql:
        create_sql += f"\n{storage_sql}"

    partition_sql = _read_gp_partition_clause(connection_key, str(oid))
    if partition_sql:
        create_sql += f"\n{partition_sql}"

    distribution_sql = _read_gp_distribution_clause(connection_key, str(oid), columns)
    if distribution_sql:
        create_sql += f"\n{distribution_sql}"

    statements = [create_sql]
    statements.extend(
        _format_optional_statement(_optional_metadata_value(row, "index_def"))
        for row in _rows(indexes)
    )
    if table_comment:
        statements.append(
            f"COMMENT ON TABLE {qualified_name} IS {_sql_string_literal(table_comment)}"
        )
    statements.extend(
        _format_gp_column_comment(qualified_name, row)
        for row in _rows(columns)
        if _optional_metadata_value(row, "column_comment")
    )
    return ";\n".join(statement for statement in statements if statement) + ";"


def _build_gp_relation_query(table_name: str) -> str:
    table_literal = _sql_string_literal(table_name)
    return f"""
SELECT
    c.oid::text AS oid,
    n.nspname AS schema_name,
    c.relname AS relation_name,
    c.relkind::text AS relkind,
    c.reloptions AS reloptions,
    pg_catalog.obj_description(c.oid, 'pg_class') AS table_comment
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n
  ON n.oid = c.relnamespace
WHERE c.oid = pg_catalog.to_regclass({table_literal})::oid
  AND c.relkind IN ('r', 'p')
""".strip()


def _build_gp_columns_query(oid: str) -> str:
    return f"""
SELECT
    a.attnum::integer AS attnum,
    a.attname AS column_name,
    pg_catalog.format_type(a.atttypid, a.atttypmod) AS formatted_type,
    pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS default_expr,
    a.attnotnull AS is_not_null,
    pg_catalog.col_description(a.attrelid, a.attnum) AS column_comment
FROM pg_catalog.pg_attribute AS a
LEFT JOIN pg_catalog.pg_attrdef AS d
  ON d.adrelid = a.attrelid
  AND d.adnum = a.attnum
WHERE a.attrelid = {oid}::oid
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY a.attnum
""".strip()


def _build_gp_constraints_query(oid: str) -> str:
    return f"""
SELECT
    conname AS constraint_name,
    contype::text AS constraint_type,
    pg_catalog.pg_get_constraintdef(oid, true) AS constraint_def
FROM pg_catalog.pg_constraint
WHERE conrelid = {oid}::oid
ORDER BY
    CASE contype
        WHEN 'p' THEN 1
        WHEN 'u' THEN 2
        WHEN 'c' THEN 3
        WHEN 'f' THEN 4
        ELSE 5
    END,
    conname
""".strip()


def _build_gp_inherits_query(oid: str) -> str:
    return f"""
SELECT
    pn.nspname AS parent_schema,
    pc.relname AS parent_table
FROM pg_catalog.pg_inherits AS i
JOIN pg_catalog.pg_class AS pc
  ON pc.oid = i.inhparent
JOIN pg_catalog.pg_namespace AS pn
  ON pn.oid = pc.relnamespace
WHERE i.inhrelid = {oid}::oid
ORDER BY i.inhseqno
""".strip()


def _build_gp_indexes_query(oid: str) -> str:
    return f"""
SELECT
    ic.relname AS index_name,
    pg_catalog.pg_get_indexdef(i.indexrelid) AS index_def
FROM pg_catalog.pg_index AS i
JOIN pg_catalog.pg_class AS ic
  ON ic.oid = i.indexrelid
WHERE i.indrelid = {oid}::oid
  AND NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_constraint AS con
      WHERE con.conindid = i.indexrelid
  )
ORDER BY ic.relname
""".strip()


def _build_gp_partition_capability_query() -> str:
    return """
SELECT
    EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS p
        JOIN pg_catalog.pg_namespace AS n
          ON n.oid = p.pronamespace
        WHERE n.nspname = 'pg_catalog'
          AND p.proname = 'pg_get_partkeydef'
    ) AS has_partkeydef,
    EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS p
        JOIN pg_catalog.pg_namespace AS n
          ON n.oid = p.pronamespace
        WHERE n.nspname = 'pg_catalog'
          AND p.proname = 'pg_get_partition_def'
    ) AS has_partition_def
""".strip()


def _build_gp_partkey_query(oid: str) -> str:
    return f"SELECT pg_catalog.pg_get_partkeydef({oid}::oid) AS partition_def"


def _build_gp_partition_def_query(oid: str) -> str:
    return f"SELECT pg_catalog.pg_get_partition_def({oid}::oid, true) AS partition_def"


def _build_gp_distribution_query(
    oid: str,
    *,
    attr_column: str,
    include_policy_type: bool,
) -> str:
    policy_type_sql = (
        "policytype::text AS policy_type,"
        if include_policy_type
        else "NULL::text AS policy_type,"
    )
    return f"""
SELECT
    {policy_type_sql}
    {attr_column}::text AS attrnums
FROM gp_distribution_policy
WHERE localoid = {oid}::oid
""".strip()


def _validate_tables(tables: str | Sequence[str]) -> list[str]:
    if isinstance(tables, str):
        return [_validate_table_name(tables)]
    if isinstance(tables, (bytes, bytearray)) or not isinstance(tables, Sequence):
        raise TypeError("tables must be a string or a sequence of strings.")
    if not tables:
        raise InvalidSqlInputError("tables must not be empty.")

    return [_validate_table_name(table_name) for table_name in tables]


def _validate_table_name(table_name: str) -> str:
    if not isinstance(table_name, str):
        raise TypeError("tables values must be strings.")

    normalized = table_name.strip()
    if not normalized:
        raise InvalidSqlInputError("tables values must not be empty.")
    return normalized


def _first_result_value(result: pd.DataFrame, table_name: str) -> str:
    if result.empty or len(result.columns) == 0:
        raise ValueError(f"No DDL returned for table {table_name}.")

    value = result.iat[0, 0]
    if pd.isna(value):
        raise ValueError(f"No DDL returned for table {table_name}.")
    return str(value)


def _normalize_ddl(ddl: str, table_name: str) -> str:
    normalized = ddl.strip().rstrip(";").rstrip()
    if not normalized:
        raise ValueError(f"No DDL returned for table {table_name}.")
    return f"{normalized};"


def _read_single_row(
    connection_key: str,
    query: str,
    table_name: str,
) -> pd.Series[Any]:
    result = cast(pd.DataFrame, read_sql(connection_key, query))
    if result.empty:
        raise ValueError(f"No metadata returned for table {table_name}.")
    return result.iloc[0]


def _rows(result: pd.DataFrame) -> list[pd.Series[Any]]:
    return [row for _, row in result.iterrows()]


def _require_metadata_value(
    row: pd.Series[Any],
    key: str,
    table_name: str,
) -> str:
    value = _optional_metadata_value(row, key)
    if value is None:
        raise ValueError(f"No metadata field {key} returned for table {table_name}.")
    return value


def _optional_metadata_value(row: pd.Series[Any], key: str) -> str | None:
    if key not in row:
        return None
    value = row[key]
    if _is_missing_value(value):
        return None
    return str(value)


def _is_missing_value(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _format_gp_column_definition(row: pd.Series[Any]) -> str:
    column_name = _require_metadata_value(row, "column_name", "<metadata>")
    formatted_type = _require_metadata_value(row, "formatted_type", "<metadata>")
    default_expr = _optional_metadata_value(row, "default_expr")
    is_not_null = _metadata_bool(row, "is_not_null")
    parts = [quote_identifier(column_name, "gp"), formatted_type]
    if default_expr:
        parts.extend(["DEFAULT", default_expr])
    if is_not_null:
        parts.append("NOT NULL")
    return " ".join(parts)


def _format_gp_constraint_definition(row: pd.Series[Any]) -> str:
    constraint_name = _optional_metadata_value(row, "constraint_name")
    constraint_def = _require_metadata_value(row, "constraint_def", "<metadata>")
    if constraint_name:
        return (
            f"CONSTRAINT {quote_identifier(constraint_name, 'gp')} {constraint_def}"
        )
    return constraint_def


def _format_gp_inherits_clause(parents: pd.DataFrame) -> str:
    parent_names = [
        _quote_gp_table_name(
            _require_metadata_value(row, "parent_schema", "<metadata>"),
            _require_metadata_value(row, "parent_table", "<metadata>"),
        )
        for row in _rows(parents)
    ]
    if not parent_names:
        return ""
    return "INHERITS (" + ", ".join(parent_names) + ")"


def _format_gp_storage_clause(reloptions: str | None) -> str:
    options = _parse_pg_array_text(reloptions)
    if not options:
        return ""
    return "WITH (\n    " + ",\n    ".join(options) + "\n)"


def _read_gp_partition_clause(connection_key: str, oid: str) -> str:
    capabilities = cast(
        pd.DataFrame,
        read_sql(connection_key, _build_gp_partition_capability_query()),
    )
    if capabilities.empty:
        return ""
    capability = capabilities.iloc[0]
    if _metadata_bool(capability, "has_partkeydef"):
        result = cast(pd.DataFrame, read_sql(connection_key, _build_gp_partkey_query(oid)))
        return _format_gp_partition_clause(_first_optional_value(result, "partition_def"))
    if _metadata_bool(capability, "has_partition_def"):
        result = cast(
            pd.DataFrame,
            read_sql(connection_key, _build_gp_partition_def_query(oid)),
        )
        return _format_gp_partition_clause(_first_optional_value(result, "partition_def"))
    return ""


def _format_gp_partition_clause(partition_def: str | None) -> str:
    if not partition_def:
        return ""
    normalized = partition_def.strip().rstrip(";")
    if not normalized:
        return ""
    if normalized.upper().startswith("PARTITION"):
        return normalized
    return f"PARTITION BY {normalized}"


def _read_gp_distribution_clause(
    connection_key: str,
    oid: str,
    columns: pd.DataFrame,
) -> str:
    for attr_column, include_policy_type in (
        ("attrnums", True),
        ("attrnums", False),
        ("distkey", True),
        ("distkey", False),
    ):
        try:
            result = cast(
                pd.DataFrame,
                read_sql(
                    connection_key,
                    _build_gp_distribution_query(
                        oid,
                        attr_column=attr_column,
                        include_policy_type=include_policy_type,
                    ),
                ),
            )
        except Exception as exc:
            if _is_missing_catalog_shape_error(exc):
                continue
            raise
        return _format_gp_distribution_clause(result, columns)
    return ""


def _format_gp_distribution_clause(result: pd.DataFrame, columns: pd.DataFrame) -> str:
    if result.empty:
        return ""
    row = result.iloc[0]
    policy_type = (_optional_metadata_value(row, "policy_type") or "").lower()
    if policy_type in {"r", "replicated"}:
        return "DISTRIBUTED REPLICATED"

    attrnums = _parse_attrnums(_optional_metadata_value(row, "attrnums"))
    if not attrnums:
        return "DISTRIBUTED RANDOMLY"

    column_by_attnum = {
        int(float(_require_metadata_value(column, "attnum", "<metadata>"))): (
            _require_metadata_value(column, "column_name", "<metadata>")
        )
        for column in _rows(columns)
    }
    distribution_columns = [
        column_by_attnum[attrnum]
        for attrnum in attrnums
        if attrnum in column_by_attnum
    ]
    if not distribution_columns:
        return "DISTRIBUTED RANDOMLY"
    quoted_columns = ", ".join(
        quote_identifier(column_name, "gp") for column_name in distribution_columns
    )
    return f"DISTRIBUTED BY ({quoted_columns})"


def _format_optional_statement(statement: str | None) -> str:
    if not statement:
        return ""
    return statement.strip().rstrip(";")


def _format_gp_column_comment(qualified_name: str, row: pd.Series[Any]) -> str:
    column_comment = _optional_metadata_value(row, "column_comment")
    if not column_comment:
        return ""
    column_name = _require_metadata_value(row, "column_name", "<metadata>")
    return (
        f"COMMENT ON COLUMN {qualified_name}.{quote_identifier(column_name, 'gp')} "
        f"IS {_sql_string_literal(column_comment)}"
    )


def _first_optional_value(result: pd.DataFrame, column: str) -> str | None:
    if result.empty or column not in result:
        return None
    value = result.iloc[0][column]
    if _is_missing_value(value):
        return None
    return str(value)


def _metadata_bool(row: pd.Series[Any], key: str) -> bool:
    if key not in row:
        return False
    value = row[key]
    if _is_missing_value(value):
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "t", "true", "y", "yes"}


def _parse_pg_array_text(value: str | None) -> list[str]:
    if not value:
        return []
    text = value.strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        return [
            item.strip().strip("'\"")
            for item in text[1:-1].split(",")
            if item.strip()
        ]
    if text.startswith("{") and text.endswith("}"):
        return [
            item.strip().strip('"')
            for item in text[1:-1].split(",")
            if item.strip()
        ]
    return [text]


def _parse_attrnums(value: str | None) -> list[int]:
    if not value:
        return []
    return [int(match) for match in re.findall(r"-?\d+", value) if int(match) > 0]


def _quote_gp_table_name(schema_name: str, relation_name: str) -> str:
    return (
        f"{quote_identifier(schema_name, 'gp')}."
        f"{quote_identifier(relation_name, 'gp')}"
    )


def _is_missing_pg_get_tabledef_error(exc: Exception) -> bool:
    message = _exception_text(exc)
    return (
        _has_sqlstate_or_class(exc, {"42883"}, {"UndefinedFunction"})
        and "pg_get_tabledef" in message
    )


def _is_missing_catalog_shape_error(exc: Exception) -> bool:
    return _has_sqlstate_or_class(
        exc,
        {"42703", "42P01", "42704"},
        {"UndefinedColumn", "UndefinedTable", "UndefinedObject"},
    )


def _has_sqlstate_or_class(
    exc: Exception,
    sqlstates: set[str],
    class_names: set[str],
) -> bool:
    exc_sqlstate = str(
        getattr(exc, "pgcode", "") or getattr(exc, "sqlstate", "")
    ).strip()
    if exc_sqlstate in sqlstates:
        return True
    return bool({cls.__name__ for cls in type(exc).mro()} & class_names)


def _exception_text(exc: Exception) -> str:
    return " ".join(str(part) for part in exc.args if part).lower() or str(exc).lower()


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


__all__ = ["extract_ddl"]
