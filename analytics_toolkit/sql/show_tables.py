from __future__ import annotations

from typing import cast

import pandas as pd
import sqlparse

from .connection.config import ConnectionConfig, TrinoConfig, get_connection_config
from .connection.errors import InvalidSqlInputError, UnsupportedConnectionTypeError
from .dml.io.read_sql import read_sql
from .operation_runner import timed_public_sql_function


_SHOW_TABLES_COLUMNS = ["db", "schema", "table_name"]


@timed_public_sql_function
def show_tables(
    db_key: str,
    schema: str | None = None,
    conditions: str | None = None,
) -> pd.DataFrame:
    """Return backend table metadata as db, schema, and table_name columns."""

    config = get_connection_config(db_key)
    schema_filter = _validate_optional_string(schema, "schema")
    conditions_filter = _validate_conditions(conditions)
    query = _build_show_tables_query(config, schema_filter, conditions_filter)

    result = cast(pd.DataFrame, read_sql(config.connection_key, query))
    return result.loc[:, _SHOW_TABLES_COLUMNS].copy()


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
    name AS table_name
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
    table_name
FROM information_schema.tables
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
    table_name
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


__all__ = ["show_tables"]
