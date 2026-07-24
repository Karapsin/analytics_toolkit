from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

from ..backends import get_backend_adapter
from ..connection.config import get_connection_config
from ..connection.errors import InvalidSqlInputError
from ..dml.io.read_sql import read_sql
from ..execution.operation_runner import timed_public_sql_function


@timed_public_sql_function
def extract_ddl(db_key: str, tables: str | Sequence[str]) -> str:
    """Return semicolon-terminated table DDL statements joined by newlines."""

    config = get_connection_config(db_key)
    adapter = get_backend_adapter(config.backend)
    table_names = _validate_tables(tables)
    ddl_statements = [
        _normalize_ddl(
            adapter.extract_table_ddl(
                config.connection_key,
                table_name,
                read_sql=read_sql,
            ),
            table_name,
        )
        for table_name in table_names
    ]
    return "\n".join(ddl_statements)


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


def _normalize_ddl(ddl: str, table_name: str) -> str:
    normalized = ddl.strip().rstrip(";").rstrip()
    if not normalized:
        raise ValueError(f"No DDL returned for table {table_name}.")
    return f"{normalized};"


def _first_result_value(result: pd.DataFrame, table_name: str) -> str:
    if result.empty or len(result.columns) == 0:
        raise ValueError(f"No DDL returned for table {table_name}.")

    value = result.iat[0, 0]
    if pd.isna(value):
        raise ValueError(f"No DDL returned for table {table_name}.")
    return str(value)


__all__ = ["extract_ddl"]
