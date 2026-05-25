from __future__ import annotations

import math
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


@timed_public_sql_function
def show_tables(
    db_key: str,
    schema: str | None = None,
    conditions: str | None = None,
) -> pd.DataFrame:
    """Return backend table metadata with row_count and table_size columns."""

    config = get_connection_config(db_key)
    schema_filter = _validate_optional_string(schema, "schema")
    conditions_filter = _validate_conditions(conditions)
    query = _build_show_tables_query(config, schema_filter, conditions_filter)

    result = cast(pd.DataFrame, read_sql(config.connection_key, query))
    normalized = result.copy()
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
    conditions: str | None,
) -> str:
    if config.backend == "ch":
        return _build_clickhouse_show_tables_query(schema, conditions)
    if config.backend == "gp":
        return _build_gp_show_tables_query(schema, conditions)
    if config.backend == "trino":
        if not isinstance(config, TrinoConfig) or not config.catalog:
            raise ValueError(
                "show_tables for Trino requires "
                f".connections['{config.connection_key}'].catalog."
            )
        return _build_trino_show_tables_query(config.catalog, schema, conditions)

    raise UnsupportedConnectionTypeError(
        "Unsupported connection type. Expected one of: 'trino', 'gp', 'ch'."
    )


def _build_clickhouse_show_tables_query(
    schema: str | None,
    conditions: str | None,
) -> str:
    filters = _schema_and_conditions_filters("database", schema, conditions)
    return f"""
SELECT
    database AS db,
    database AS schema,
    name AS table_name,
    total_rows AS row_count,
    total_bytes AS table_size_bytes
FROM system.tables
WHERE 1 = 1{_format_filter_lines(filters)}
ORDER BY database, name
""".strip()


def _build_gp_show_tables_query(
    schema: str | None,
    conditions: str | None,
) -> str:
    filters = _schema_and_conditions_filters("table_schema", schema, conditions)
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
    conditions: str | None,
) -> str:
    filters = _schema_and_conditions_filters("table_schema", schema, conditions)
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


def _schema_and_conditions_filters(
    schema_column: str,
    schema: str | None,
    conditions: str | None,
) -> list[str]:
    filters: list[str] = []
    if schema is not None:
        filters.append(f"{schema_column} = {_sql_string_literal(schema)}")
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
