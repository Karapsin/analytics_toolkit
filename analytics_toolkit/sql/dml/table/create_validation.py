"""Input validation shared by query-backed table creation entrypoints."""

from __future__ import annotations

import sqlparse

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError


def normalize_table_name(table_name: str) -> str:
    normalized = table_name.strip()
    if not normalized:
        message = "table_name must not be empty."
        raise InvalidSqlInputError(message)
    return normalized


def normalize_single_query(query: str) -> str:
    normalized = query.strip()
    if not normalized:
        message = "sql must not be empty."
        raise InvalidSqlInputError(message)
    statements = [
        statement.strip().rstrip(";").rstrip()
        for statement in sqlparse.split(normalized)
        if statement.strip()
    ]
    if len(statements) != 1:
        message = "create_table_from_sql expects exactly one SQL statement."
        raise InvalidSqlInputError(message)
    return statements[0]
