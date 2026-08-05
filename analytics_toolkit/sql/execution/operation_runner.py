from __future__ import annotations

import time
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Dict, TypeVar, cast

from analytics_toolkit.general import time_print, time_print_context
from analytics_toolkit.sql._log_context import current_sql_log_context

from ..connection.errors import SqlOperationContext, annotate_sql_exception
from .cancellation import raise_if_cancelled
from .plans import SqlOperationMetadata
from .validation import validate_non_negative_number, validate_positive_int

T = TypeVar("T")
ConnectionRef = Dict[str, Any]


def timed_public_sql_function(function: Callable[..., T]) -> Callable[..., T]:
    """Print total elapsed time for a public SQL API function."""
    if getattr(function, "__sql_public_timing__", False):
        return function

    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        started_at = time.perf_counter()
        with time_print_context(operation=function.__name__):
            try:
                return function(*args, **kwargs)
            finally:
                elapsed_seconds = time.perf_counter() - started_at
                time_print(
                    f"Finished SQL function in {_format_duration(elapsed_seconds)}",
                    operation=function.__name__,
                    phase="timing",
                )

    setattr(wrapper, "__sql_public_timing__", True)
    return cast(Callable[..., T], wrapper)


@contextmanager
def tracked_sql_operation(
    *,
    metadata: SqlOperationMetadata | None = None,
    operation_name: str,
    alias: str | None,
    backend: str | None,
    phase: str | None,
    retry_attempt: int | None = None,
    query_label: str | None = None,
    preview_sql: str | None = None,
) -> Any:
    """Record elapsed time and visible status messages for SQL operations."""
    operation_metadata = metadata or SqlOperationMetadata()
    operation_metadata.query_label = query_label
    if retry_attempt is not None:
        operation_metadata.retry_attempts = retry_attempt

    started_at = time.perf_counter()
    time_print(
        "Starting SQL",
        operation=operation_name,
        connection=alias,
        backend=backend,
        phase=phase,
    )
    try:
        with time_print_context(
            operation=operation_name,
            connection=alias,
            backend=backend,
            phase=phase,
        ):
            yield operation_metadata
    except Exception:
        operation_metadata.operation_status = "failed"
        raise
    else:
        operation_metadata.operation_status = "success"
    finally:
        operation_metadata.elapsed_seconds = time.perf_counter() - started_at
        status = operation_metadata.operation_status or "finished"
        message_prefix = "Failed SQL" if status == "failed" else "Finished SQL"
        time_print(
            f"{message_prefix} in {_format_duration(operation_metadata.elapsed_seconds)}",
            operation=operation_name,
            connection=alias,
            backend=backend,
            phase=phase,
        )
        _log_prefix, suppress_sql = current_sql_log_context()
        preview_line = None if suppress_sql else _first_non_empty_sql_line(preview_sql)
        if preview_line is not None:
            time_print(
                f"Finished SQL statement:\n{preview_line}",
                operation=operation_name,
                connection=alias,
                backend=backend,
                phase=phase,
            )


def _format_duration(seconds: float) -> str:
    normalized_seconds = max(float(seconds), 0.0)
    if normalized_seconds < 1:
        rounded = round(normalized_seconds, 3)
        if rounded <= 0:
            return "0 seconds"
        if rounded >= 1:
            return "1 second"
        value = f"{rounded:.3f}".rstrip("0").rstrip(".")
        return f"{value} seconds"

    remaining_seconds = int(normalized_seconds + 0.5)
    units = (
        ("day", 24 * 60 * 60),
        ("hour", 60 * 60),
        ("minute", 60),
        ("second", 1),
    )
    parts: list[str] = []
    for unit_name, unit_seconds in units:
        value, remaining_seconds = divmod(remaining_seconds, unit_seconds)
        if value:
            suffix = "" if value == 1 else "s"
            parts.append(f"{value} {unit_name}{suffix}")
    return " ".join(parts) if parts else "0 seconds"


def merge_operation_metadata(
    metadata: SqlOperationMetadata,
    *,
    elapsed_seconds: float | None = None,
    retry_attempts: int | None = None,
    read_rows: int | None = None,
    statement_count: int | None = None,
    operation_status: str | None = None,
    query_label: str | None = None,
) -> SqlOperationMetadata:
    if elapsed_seconds is not None:
        metadata.elapsed_seconds = elapsed_seconds
    if retry_attempts is not None:
        metadata.retry_attempts = retry_attempts
    if read_rows is not None:
        metadata.read_rows = read_rows
    if statement_count is not None:
        metadata.statement_count = statement_count
    if operation_status is not None:
        metadata.operation_status = operation_status
    if query_label is not None:
        metadata.query_label = query_label
    return metadata


def validate_retry_options(
    retry_cnt: int,
    timeout_increment: int | float,
) -> None:
    try:
        validate_positive_int(retry_cnt, "retry_cnt")
    except ValueError as exc:
        raise ValueError("retry_cnt must be an integer of at least 1.") from exc
    try:
        validate_non_negative_number(timeout_increment, "timeout_increment")
    except ValueError as exc:
        raise ValueError("timeout_increment must be a finite non-negative number.") from exc


def validate_progress_option(progress: bool) -> None:
    if not isinstance(progress, bool):
        raise ValueError("progress must be a boolean.")


def run_connection_operation(
    *,
    operation_name: str,
    connection_key: str,
    backend: str,
    retry_cnt: int,
    timeout_increment: int | float,
    open_connection: Callable[[str], Any],
    operation: Callable[[ConnectionRef, int], T],
    context_factory: Callable[[int], SqlOperationContext],
    cleanup: Callable[[ConnectionRef], None] | None = None,
) -> T:
    """Run a public SQL operation with a fresh connection for each retry."""

    def attempt_operation(attempt: int) -> T:
        raise_if_cancelled()
        connection_ref: ConnectionRef = {"connection": open_connection(connection_key)}
        try:
            raise_if_cancelled()
            return operation(connection_ref, attempt)
        except Exception as exc:
            annotate_sql_exception(exc, context_factory(attempt))
            from ..backends import get_backend_adapter

            get_backend_adapter(backend).rollback_quietly(connection_ref["connection"])
            raise
        finally:
            if cleanup is None:
                _close_connection(connection_ref, connection_key, backend=backend)
            else:
                cleanup(connection_ref)

    return _run_with_retry(
        operation_name=operation_name,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        operation=attempt_operation,
    )


def run_retrying_operation(
    *,
    operation_name: str,
    retry_cnt: int,
    timeout_increment: int | float,
    operation: Callable[[int], T],
    context_factory: Callable[[int], SqlOperationContext],
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    safe_exception_logging: bool = False,
) -> T:
    def annotated_operation(attempt: int) -> T:
        try:
            return operation(attempt)
        except Exception as exc:
            annotate_sql_exception(exc, context_factory(attempt))
            raise

    return _run_with_retry(
        operation_name=operation_name,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        operation=annotated_operation,
        retryable_exceptions=retryable_exceptions,
        safe_exception_logging=safe_exception_logging,
    )


def run_annotated_once(
    *,
    operation: Callable[[], T],
    context: SqlOperationContext,
) -> T:
    try:
        return operation()
    except Exception as exc:
        annotate_sql_exception(exc, context)
        raise


def _close_connection(
    connection_ref: ConnectionRef,
    connection_key: str,
    *,
    backend: str | None = None,
) -> None:
    time_print(
        "Closing connection",
        connection=connection_key,
        backend=backend,
        phase="close",
    )
    connection_ref["connection"].close()


def _first_non_empty_sql_line(sql: str | None) -> str | None:
    if sql is None:
        return None
    for line in str(sql).splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _run_with_retry(**kwargs: Any) -> Any:
    from ..dml.transfer.runtime.retry import run_with_retry

    return run_with_retry(**kwargs)


def _rollback_quietly(connection: Any) -> None:
    from ..dml.transfer.runtime.retry import rollback_quietly

    rollback_quietly(connection)
