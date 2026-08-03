from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.connection.errors import SqlConfigError
from analytics_toolkit.sql.execution.cancellation import (
    current_cancellation_scope,
    raise_if_cancelled,
    wait_or_raise_if_cancelled,
)

from ....connection.get_sql_connection import get_sql_connection
from ....execution.operation_runner import _format_duration

T = TypeVar("T")

_NON_RETRYABLE_CLICKHOUSE_ERROR_NAMES = {
    "ALREADY_EXISTS",
    "BAD_ARGUMENTS",
    "COLUMN_NOT_FOUND",
    "FEATURE_NOT_SUPPORTED",
    "FUNCTION_NOT_FOUND",
    "ILLEGAL_TYPE_OF_ARGUMENT",
    "INSUFFICIENT_PRIVILEGE",
    "SCHEMA_NOT_FOUND",
    "SYNTAX_ERROR",
    "TABLE_NOT_FOUND",
    "TYPE_MISMATCH",
    "UNKNOWN_IDENTIFIER",
    "UNKNOWN_TABLE",
}


def run_with_retry(
    operation_name: str,
    retry_cnt: int,
    timeout_increment: float,
    operation: Callable[[int], Any],
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    non_retryable_predicate: Callable[[Exception], bool] | None = None,
) -> Any:
    last_error: Exception | None = None
    should_not_retry = non_retryable_predicate or is_non_retryable_sql_error

    for attempt in range(1, retry_cnt + 1):
        raise_if_cancelled()
        try:
            return operation(attempt)
        except Exception as exc:
            if not isinstance(exc, retryable_exceptions):
                raise
            if should_not_retry(exc):
                time_print(
                    f"Failed with a non-retryable error: {exc!r}",
                    level="warning",
                    operation=operation_name,
                    phase="retry",
                )
                raise
            last_error = exc
            if attempt >= retry_cnt:
                time_print(
                    f"Failed after {attempt} attempt(s): {exc!r}",
                    level="warning",
                    operation=operation_name,
                    phase="retry",
                )
                break

            sleep_seconds = attempt * timeout_increment
            time_print(
                f"Failed on attempt {attempt}/{retry_cnt}: {exc!r}",
                level="warning",
                operation=operation_name,
                phase="retry",
            )
            time_print(
                f"Retrying in {_format_duration(sleep_seconds)}",
                operation=operation_name,
                phase="retry",
            )
            if sleep_seconds > 0:
                if current_cancellation_scope() is None:
                    time.sleep(sleep_seconds)
                else:
                    wait_or_raise_if_cancelled(sleep_seconds)

    if last_error is None:
        raise RuntimeError(f"{operation_name} failed without capturing an exception.")
    raise last_error.with_traceback(last_error.__traceback__)


def is_non_retryable_sql_error(exc: Exception) -> bool:
    """Return True for deterministic SQL errors that another attempt won't fix."""
    if (
        getattr(exc, "analytics_toolkit_sql_retry_safe", True) is False
        or isinstance(exc, SqlConfigError)
    ):
        return True
    class_names = _exception_class_names(exc)
    if class_names & {
        "SyntaxError",
        "UndefinedObject",
        "UndefinedTable",
        "UndefinedColumn",
        "UndefinedFunction",
        "AmbiguousColumn",
        "InvalidColumnReference",
        "InvalidTableDefinition",
        "InvalidSchemaName",
        "FeatureNotSupported",
        "GroupingError",
        "InsufficientPrivilege",
        "SchemaNotFoundError",
        "TableNotFoundError",
    }:
        return True

    sqlstate = str(getattr(exc, "pgcode", "") or getattr(exc, "sqlstate", "")).strip()
    if sqlstate in {
        "42601",  # syntax_error
        "42704",  # undefined_object
        "42P01",  # undefined_table
        "42703",  # undefined_column
        "42883",  # undefined_function
        "42702",  # ambiguous_column
        "42803",  # grouping_error
        "3F000",  # invalid_schema_name
        "0A000",  # feature_not_supported
        "42P07",  # duplicate_table
        "42501",  # insufficient_privilege
    }:
        return True

    error_name = str(getattr(exc, "error_name", "") or getattr(exc, "name", "")).strip().upper()
    if error_name in _NON_RETRYABLE_CLICKHOUSE_ERROR_NAMES:
        return True

    message = _exception_message(exc)
    if (
        _is_non_retryable_clickhouse_error_name(message)
        or _is_clickhouse_conversion_error(message)
        or any(pattern in message for pattern in _NON_RETRYABLE_MESSAGE_PATTERNS)
    ):
        return True
    missing_object = "does not exist" in message or "doesn't exist" in message
    return missing_object and ("table" in message or "type " in message)


def _exception_class_names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).mro()}


def _exception_message(exc: BaseException) -> str:
    return " ".join(str(part) for part in exc.args if part).lower() or str(exc).lower()


def _is_clickhouse_conversion_error(message: str) -> bool:
    is_server_error = (
        "received clickhouse exception" in message or "db::exception" in message
    )
    return is_server_error and (
        "cannot parse" in message or "cannot convert" in message
    )


def _is_non_retryable_clickhouse_error_name(message: str) -> bool:
    is_server_error = (
        "received clickhouse exception" in message or "db::exception" in message
    )
    return is_server_error and any(
        f"({error_name.lower()})" in message
        for error_name in _NON_RETRYABLE_CLICKHOUSE_ERROR_NAMES
    )


_NON_RETRYABLE_MESSAGE_PATTERNS = (
    "sql must not return duplicate columns:",
    "trino table operations for schema-qualified names require",
    "trino table operations for unqualified names require",
    "syntax error",
    "syntax_error",
    "mismatched input",
    "table not found",
    "table_not_found",
    "relation does not exist",
    "unknown table",
    "unknown_identifier",
    "can't adapt type",
    "no such table",
    "undefined table",
    "undefined_table",
    "is ambiguous",
    "must appear in the group by clause",
    "illegal type of argument",
    "illegal value (aggregate function) for positional argument in group by",
    "cross-database references are not implemented",
    "must be owner of relation",
    "must be owner of table",
    "permission denied",
)


def rollback_quietly(connection: Any) -> None:
    try:
        connection.rollback()
    except Exception:
        return


def replace_connection(connection_key: str, connection_ref: dict[str, Any]) -> None:
    try:
        connection_ref["connection"].close()
    except Exception:
        pass
    connection_ref["connection"] = get_sql_connection(connection_key)


def run_with_fresh_connection(
    connection_key: str,
    role: str,
    operation: Callable[[dict[str, Any]], T],
    open_connection: Callable[[str], Any] = get_sql_connection,
) -> T:
    connection_ref: dict[str, Any] = {"connection": open_connection(connection_key)}
    try:
        return operation(connection_ref)
    finally:
        close_connection_ref(connection_ref, connection_key, role)


def close_connection_ref(
    connection_ref: dict[str, Any],
    connection_type: str,
    role: str,
) -> None:
    connection = connection_ref.get("connection")
    if connection is None:
        return
    time_print(
        "Closing connection",
        connection=connection_type,
        phase=f"close_{role}",
    )
    try:
        connection.close()
    except Exception:
        time_print(
            "Failed",
            level="warning",
            connection=connection_type,
            phase=f"close_{role}",
        )
