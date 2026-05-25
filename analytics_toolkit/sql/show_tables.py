from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math
import re
from typing import cast

import pandas as pd
import sqlparse

from .connection.config import ConnectionConfig, TrinoConfig, get_connection_config
from .connection.errors import InvalidSqlInputError, UnsupportedConnectionTypeError
from .dml.io.read_sql import read_sql
from .operation_runner import timed_public_sql_function


_SHOW_TABLES_COLUMNS = ["db", "schema", "table_name", "row_count", "table_size"]
_ROW_COUNT_COLUMN = "row_count"
_TABLE_SIZE_BYTES_COLUMN = "table_size_bytes"
_TABLE_SIZE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


@dataclass(frozen=True)
class _ClickHouseShardTable:
    cluster: str
    database: str
    table: str


@timed_public_sql_function
def show_tables(
    db_key: str,
    schema: str | None = None,
    conditions: str | None = None,
    table_name: str | Sequence[str] | None = None,
    ch_distributed_table_stats: bool = False,
) -> pd.DataFrame:
    """Return backend table metadata with row_count and table_size columns."""

    _validate_bool(
        ch_distributed_table_stats,
        "ch_distributed_table_stats",
    )
    config = get_connection_config(db_key)
    schema_filter = _validate_optional_string(schema, "schema")
    table_name_filter = _validate_table_names(table_name, schema_filter)
    conditions_filter = _validate_conditions(conditions)
    query = _build_show_tables_query(
        config,
        schema_filter,
        table_name_filter,
        conditions_filter,
        ch_distributed_table_stats=ch_distributed_table_stats,
    )

    result = cast(pd.DataFrame, read_sql(config.connection_key, query))
    if result.empty:
        return pd.DataFrame(columns=_SHOW_TABLES_COLUMNS)

    normalized = result.copy()
    if config.backend == "ch" and ch_distributed_table_stats:
        normalized = _apply_clickhouse_shard_stats(
            config.connection_key,
            normalized,
        )
    normalized["row_count"] = pd.Series(
        (_normalize_row_count(value) for value in normalized[_ROW_COUNT_COLUMN]),
        index=normalized.index,
        dtype=object,
    )
    normalized["table_size"] = normalized[_TABLE_SIZE_BYTES_COLUMN].map(
        _format_table_size,
    )
    return normalized.loc[:, _SHOW_TABLES_COLUMNS].copy()


def _build_show_tables_query(
    config: ConnectionConfig,
    schema: str | None,
    table_names: list[str] | None,
    conditions: str | None,
    *,
    ch_distributed_table_stats: bool = False,
) -> str:
    if config.backend == "ch":
        return _build_clickhouse_show_tables_query(
            schema,
            table_names,
            conditions,
            include_distributed_metadata=ch_distributed_table_stats,
        )
    if config.backend == "gp":
        return _build_gp_show_tables_query(schema, table_names, conditions)
    if config.backend == "trino":
        if not isinstance(config, TrinoConfig) or not config.catalog:
            raise ValueError(
                "show_tables for Trino requires "
                f".connections['{config.connection_key}'].catalog."
            )
        return _build_trino_show_tables_query(
            config.catalog,
            schema,
            table_names,
            conditions,
        )

    raise UnsupportedConnectionTypeError(
        "Unsupported connection type. Expected one of: 'trino', 'gp', 'ch'."
    )


def _build_clickhouse_show_tables_query(
    schema: str | None,
    table_names: list[str] | None,
    conditions: str | None,
    *,
    include_distributed_metadata: bool = False,
) -> str:
    filters = _metadata_filters("database", schema, "name", table_names, conditions)
    distributed_metadata_columns = (
        ",\n    engine,\n    engine_full"
        if include_distributed_metadata
        else ""
    )
    return f"""
SELECT
    database AS db,
    database AS schema,
    name AS table_name,
    total_rows AS row_count,
    total_bytes AS table_size_bytes{distributed_metadata_columns}
FROM system.tables
WHERE 1 = 1{_format_filter_lines(filters)}
ORDER BY database, name
""".strip()


def _apply_clickhouse_shard_stats(
    connection_key: str,
    tables: pd.DataFrame,
) -> pd.DataFrame:
    if "engine" not in tables.columns or "engine_full" not in tables.columns:
        return tables

    shard_refs_by_index: dict[object, _ClickHouseShardTable] = {}
    for index, row in tables.iterrows():
        engine = row.get("engine")
        if not isinstance(engine, str) or engine.lower() != "distributed":
            continue

        shard_ref = _extract_clickhouse_distributed_shard_table(
            row.get("engine_full"),
            row.get("schema"),
        )
        if shard_ref is not None:
            shard_refs_by_index[index] = shard_ref

    if not shard_refs_by_index:
        return tables

    stats = _read_clickhouse_shard_stats(
        connection_key,
        set(shard_refs_by_index.values()),
    )
    if not stats:
        return tables

    resolved = tables.copy()
    for index, shard_ref in shard_refs_by_index.items():
        stat = stats.get(shard_ref)
        if stat is None:
            continue
        row_count, table_size_bytes = stat
        resolved.at[index, _ROW_COUNT_COLUMN] = row_count
        resolved.at[index, _TABLE_SIZE_BYTES_COLUMN] = table_size_bytes
    return resolved


def _read_clickhouse_shard_stats(
    connection_key: str,
    shard_refs: set[_ClickHouseShardTable],
) -> dict[_ClickHouseShardTable, tuple[object, object]]:
    stats: dict[_ClickHouseShardTable, tuple[object, object]] = {}
    refs_by_cluster: dict[str, set[_ClickHouseShardTable]] = {}
    original_refs_by_resolved_ref: dict[
        _ClickHouseShardTable,
        set[_ClickHouseShardTable],
    ] = {}
    for shard_ref in shard_refs:
        cluster = _resolve_clickhouse_cluster_macro(connection_key, shard_ref.cluster)
        resolved_ref = _ClickHouseShardTable(
            cluster=cluster,
            database=shard_ref.database,
            table=shard_ref.table,
        )
        refs_by_cluster.setdefault(cluster, set()).add(resolved_ref)
        original_refs_by_resolved_ref.setdefault(resolved_ref, set()).add(shard_ref)

    for cluster, cluster_refs in refs_by_cluster.items():
        query = _build_clickhouse_shard_stats_query(cluster, cluster_refs)
        try:
            result = cast(pd.DataFrame, read_sql(connection_key, query))
        except Exception:
            continue
        for _, row in result.iterrows():
            database = row.get("shard_database")
            table = row.get("shard_table")
            if not isinstance(database, str) or not isinstance(table, str):
                continue
            resolved_ref = _ClickHouseShardTable(
                cluster=cluster,
                database=database,
                table=table,
            )
            stat = (
                row.get(_ROW_COUNT_COLUMN),
                row.get(_TABLE_SIZE_BYTES_COLUMN),
            )
            stats[resolved_ref] = stat
            for original_ref in original_refs_by_resolved_ref.get(resolved_ref, ()):
                stats[original_ref] = stat
    return stats


def _build_clickhouse_shard_stats_query(
    cluster: str,
    shard_refs: set[_ClickHouseShardTable],
) -> str:
    table_filters = ", ".join(
        f"({_sql_string_literal(shard_ref.database)}, "
        f"{_sql_string_literal(shard_ref.table)})"
        for shard_ref in sorted(
            shard_refs,
            key=lambda value: (value.database, value.table),
        )
    )
    return f"""
SELECT
    database AS shard_database,
    name AS shard_table,
    sum(ifNull(total_rows, 0)) AS row_count,
    sum(ifNull(total_bytes, 0)) AS table_size_bytes
FROM cluster({_sql_string_literal(cluster)}, system, tables)
WHERE (database, name) IN ({table_filters})
GROUP BY database, name
ORDER BY database, name
""".strip()


def _resolve_clickhouse_cluster_macro(connection_key: str, cluster: str) -> str:
    macro_name = _extract_clickhouse_macro_name(cluster)
    if macro_name is None:
        return cluster

    query = (
        "SELECT "
        f"getMacro({_sql_string_literal(macro_name)}) AS cluster_name"
    )
    try:
        result = cast(pd.DataFrame, read_sql(connection_key, query))
    except Exception:
        return cluster

    if result.empty or "cluster_name" not in result.columns:
        return cluster
    value = result.iloc[0]["cluster_name"]
    if pd.isna(value):
        return cluster
    resolved = str(value).strip()
    return resolved or cluster


def _extract_clickhouse_macro_name(value: str) -> str | None:
    match = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value.strip())
    if match is None:
        return None
    return match.group(1)


def _extract_clickhouse_distributed_shard_table(
    engine_full: object,
    default_database: object,
) -> _ClickHouseShardTable | None:
    if not isinstance(engine_full, str):
        return None

    args = _extract_clickhouse_function_args(engine_full, "Distributed")
    if args is None or len(args) < 3:
        return None

    cluster = _normalize_clickhouse_engine_arg(args[0])
    database = _normalize_clickhouse_distributed_database_arg(
        args[1],
        default_database,
    )
    table = _normalize_clickhouse_engine_arg(args[2])
    if not cluster or not database or not table:
        return None
    return _ClickHouseShardTable(cluster=cluster, database=database, table=table)


def _normalize_clickhouse_distributed_database_arg(
    value: str,
    default_database: object,
) -> str | None:
    database = _normalize_clickhouse_engine_arg(value)
    if database is None:
        return None

    compact = "".join(database.split()).lower()
    if compact in {"currentdatabase()", "database()"}:
        if not isinstance(default_database, str):
            return None
        database = default_database.strip()
        if not database:
            return None
    return database


def _normalize_clickhouse_engine_arg(value: str) -> str | None:
    normalized = value.strip()
    if not normalized:
        return None

    if len(normalized) >= 2 and normalized[0] == normalized[-1]:
        if normalized[0] == "'":
            return normalized[1:-1].replace("''", "'")
        if normalized[0] in {'"', "`"}:
            return normalized[1:-1].replace(normalized[0] * 2, normalized[0])

    return normalized


def _extract_clickhouse_function_args(
    sql: str,
    function_name: str,
) -> list[str] | None:
    position = _find_clickhouse_function_call(sql, function_name)
    if position is None:
        return None

    open_paren = _skip_whitespace(sql, position + len(function_name))
    close_paren = _find_matching_paren(sql, open_paren)
    if close_paren is None:
        return None
    return _split_top_level_args(sql[open_paren + 1 : close_paren])


def _find_clickhouse_function_call(sql: str, function_name: str) -> int | None:
    name_length = len(function_name)
    index = 0
    quote: str | None = None
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            elif char == "\\" and quote == "'" and index + 1 < len(sql):
                index += 2
                continue
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue

        candidate_end = index + name_length
        if (
            sql[index:candidate_end].lower() == function_name.lower()
            and _is_clickhouse_identifier_boundary(sql, index - 1)
            and _is_clickhouse_identifier_boundary(sql, candidate_end)
            and _skip_whitespace(sql, candidate_end) < len(sql)
            and sql[_skip_whitespace(sql, candidate_end)] == "("
        ):
            return index
        index += 1
    return None


def _find_matching_paren(sql: str, open_paren: int) -> int | None:
    if open_paren >= len(sql) or sql[open_paren] != "(":
        return None

    depth = 1
    quote: str | None = None
    index = open_paren + 1
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            elif char == "\\" and quote == "'" and index + 1 < len(sql):
                index += 2
                continue
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _split_top_level_args(args_sql: str) -> list[str]:
    args: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    index = 0
    while index < len(args_sql):
        char = args_sql[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(args_sql) and args_sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            elif char == "\\" and quote == "'" and index + 1 < len(args_sql):
                index += 2
                continue
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0:
            arg = args_sql[start:index].strip()
            if arg:
                args.append(arg)
            start = index + 1
        index += 1

    arg = args_sql[start:].strip()
    if arg:
        args.append(arg)
    return args


def _skip_whitespace(sql: str, position: int) -> int:
    while position < len(sql) and sql[position].isspace():
        position += 1
    return position


def _is_clickhouse_identifier_boundary(sql: str, position: int) -> bool:
    return position < 0 or position >= len(sql) or not _is_clickhouse_identifier_char(
        sql[position],
    )


def _is_clickhouse_identifier_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _build_gp_show_tables_query(
    schema: str | None,
    table_names: list[str] | None,
    conditions: str | None,
) -> str:
    filters = _metadata_filters(
        "table_schema",
        schema,
        "table_name",
        table_names,
        conditions,
    )
    return f"""
SELECT
    current_database() AS db,
    table_schema AS schema,
    table_name,
    CASE
        WHEN c.reltuples >= 0 THEN c.reltuples::bigint
        ELSE NULL
    END AS row_count,
    CASE
        WHEN c.relkind IN ('r', 'm', 'p') THEN pg_total_relation_size(c.oid)
        ELSE NULL
    END AS table_size_bytes
FROM information_schema.tables AS t
LEFT JOIN pg_catalog.pg_namespace AS n
  ON n.nspname = t.table_schema
LEFT JOIN pg_catalog.pg_class AS c
  ON c.relnamespace = n.oid
  AND c.relname = t.table_name
WHERE 1 = 1{_format_filter_lines(filters)}
ORDER BY table_schema, table_name
""".strip()


def _build_trino_show_tables_query(
    catalog: str,
    schema: str | None,
    table_names: list[str] | None,
    conditions: str | None,
) -> str:
    filters = _metadata_filters(
        "table_schema",
        schema,
        "table_name",
        table_names,
        conditions,
    )
    return f"""
SELECT
    table_catalog AS db,
    table_schema AS schema,
    table_name,
    CAST(NULL AS BIGINT) AS row_count,
    CAST(NULL AS BIGINT) AS table_size_bytes
FROM {catalog}.information_schema.tables
WHERE 1 = 1{_format_filter_lines(filters)}
ORDER BY table_schema, table_name
""".strip()


def _metadata_filters(
    schema_column: str,
    schema: str | None,
    table_name_column: str,
    table_names: list[str] | None,
    conditions: str | None,
) -> list[str]:
    filters: list[str] = []
    if schema is not None:
        filters.append(f"{schema_column} = {_sql_string_literal(schema)}")
    if table_names is not None:
        filters.append(_table_names_filter(table_name_column, table_names))
    if conditions is not None:
        filters.append(f"({conditions})")
    return filters


def _format_filter_lines(filters: list[str]) -> str:
    return "".join(f"\n  AND {filter_sql}" for filter_sql in filters)


def _validate_optional_string(value: str | None, parameter_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{parameter_name} must be a string or None.")

    normalized = value.strip()
    if not normalized:
        raise InvalidSqlInputError(f"{parameter_name} must not be empty.")
    return normalized


def _validate_bool(value: bool, parameter_name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{parameter_name} must be a boolean.")


def _validate_table_names(
    table_name: str | Sequence[str] | None,
    schema: str | None,
) -> list[str] | None:
    if table_name is None:
        return None
    if isinstance(table_name, str):
        return [_validate_table_name_value(table_name, schema)]
    if isinstance(table_name, (bytes, bytearray)) or not isinstance(
        table_name,
        Sequence,
    ):
        raise TypeError("table_name must be a string, a sequence of strings, or None.")
    if not table_name:
        raise InvalidSqlInputError("table_name must not be empty.")

    return [
        _validate_table_name_value(value, schema)
        for value in table_name
    ]


def _validate_table_name_value(value: str, schema: str | None) -> str:
    if not isinstance(value, str):
        raise TypeError("table_name values must be strings.")
    normalized = value.strip()
    if not normalized:
        raise InvalidSqlInputError("table_name values must not be empty.")
    if schema is not None and normalized.startswith(f"{schema}."):
        normalized = normalized[len(schema) + 1 :]
        if not normalized:
            raise InvalidSqlInputError("table_name values must not be empty.")
    return normalized


def _validate_conditions(conditions: str | None) -> str | None:
    normalized = _validate_optional_string(conditions, "conditions")
    if normalized is None:
        return None

    statements = [
        statement.strip()
        for statement in sqlparse.split(normalized)
        if statement.strip()
    ]
    if len(statements) != 1:
        raise InvalidSqlInputError(
            "conditions must contain exactly one SQL expression."
        )

    expression = statements[0].rstrip(";").rstrip()
    if not expression:
        raise InvalidSqlInputError("conditions must not be empty.")
    return expression


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _table_names_filter(column: str, table_names: list[str]) -> str:
    if len(table_names) == 1:
        return f"{column} = {_sql_string_literal(table_names[0])}"
    values = ", ".join(_sql_string_literal(name) for name in table_names)
    return f"{column} IN ({values})"


def _format_table_size(value: object) -> str | None:
    if pd.isna(value):
        return None

    try:
        size = float(value)
    except (TypeError, ValueError):
        return str(value)

    if not math.isfinite(size):
        return None

    sign = "-" if size < 0 else ""
    size = abs(size)
    unit_index = 0
    while size >= 1024 and unit_index < len(_TABLE_SIZE_UNITS) - 1:
        size /= 1024
        unit_index += 1

    unit = _TABLE_SIZE_UNITS[unit_index]
    if unit == "B":
        return f"{sign}{int(round(size))} {unit}"
    return f"{sign}{size:.2f} {unit}"


def _normalize_row_count(value: object) -> int | None:
    if pd.isna(value):
        return None

    try:
        count = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(count) or count < 0:
        return None
    return int(round(count))


__all__ = ["show_tables"]
