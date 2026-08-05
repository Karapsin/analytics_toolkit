from __future__ import annotations

from typing import Any

from analytics_toolkit.sql._log_context import (
    current_sql_log_context,
    prefix_sql_log_message,
)


def execute_commands(self: Any, connection: Any, sqls: list[str]) -> None:
    for sql in sqls:
        self.execute_command(connection, sql)


def read_dataframe(
    self: Any,
    connection: Any,
    query: str,
    *,
    print_queries: bool,
    print_query: Any,
    read_dbapi_query: Any,
) -> Any:
    _time_print("Reading DataFrame", backend=self.backend)
    try:
        print_query(query, print_queries)
        return self._read_dataframe_impl(connection, query, read_dbapi_query)
    except Exception:
        _time_print(_sql_failure_message(query), backend=self.backend)
        raise


def read_dataframe_impl(
    self: Any,
    connection: Any,
    query: str,
    read_dbapi_query: Any,
) -> Any:
    return read_dbapi_query(connection, query)


def read_columns(  # noqa: PLR0913
    self: Any,
    connection: Any,
    query: str,
    *,
    print_queries: bool,
    print_query: Any,
    read_dbapi_columns: Any,
) -> Any:
    _time_print("Reading column data", backend=self.backend)
    try:
        print_query(query, print_queries)
        return self._read_columns_impl(connection, query, read_dbapi_columns)
    except Exception:
        _time_print(_sql_failure_message(query), backend=self.backend)
        raise


def read_columns_impl(
    self: Any,
    connection: Any,
    query: str,
    read_dbapi_columns: Any,
) -> Any:
    del self
    return read_dbapi_columns(connection, query)


def _time_print(message: str, *, backend: str) -> None:
    from analytics_toolkit.general import time_print

    time_print(prefix_sql_log_message(message), backend=backend)


def _sql_failure_message(query: str) -> str:
    _prefix, suppress_sql = current_sql_log_context()
    if suppress_sql:
        return "Failed SQL (details suppressed)"
    return f"Failed SQL:\n{query}"
