from __future__ import annotations

import time
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Dict, TypeVar, cast

from analytics_toolkit.general import time_print, time_print_context

from .connection.errors import SqlOperationContext, annotate_sql_exception
from .plans import SqlOperationMetadata


T = TypeVar("T")
ConnectionRef = Dict[str, Any]


def timed_public_sql_function(function: Callable[..., T]) -> Callable[..., T]:
    """Print total elapsed time for a public SQL API function."""
    if getattr(function, "__sql_public_timing__", False):
        return function

    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        started_at = time.perf_counter()
        with time_print_context(operation=function.__name__, phase="total"):
            try:
                return function(*args, **kwargs)
            finally:
                elapsed_seconds = time.perf_counter() - started_at
                time_print(
                    f"SQL function {function.__name__} execution took "
                    f"{elapsed_seconds:.3f}s",
                    operation=function.__name__,
                    phase="total",
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
    phase: str,
    retry_attempt: int | None = None,
    query_label: str | None = None,
    preview_sql: str | None = None,
) -> Any:
    """Record elapsed time and visible status messages for SQL operations."""
    operation_metadata = metadata or SqlOperationMetadata()
    operation_metadata.query_label = query_label
    if retry_attempt is not None:
        operation_metadata.retry_attempts = retry_attempt

    label_parts = [operation_name, phase]
    if alias is not None and backend is not None:
        label_parts.append(f"{alias} ({backend})")
    elif alias is not None:
        label_parts.append(alias)
    label = " on ".join(label_parts[:2])
    if len(label_parts) > 2:
        label = f"{label} for {label_parts[2]}"

    started_at = time.perf_counter()
    time_print(
        f"Starting SQL operation {label}",
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
        time_print(
            f"Finished SQL operation {label}: {status} "
            f"in {operation_metadata.elapsed_seconds:.3f}s",
            operation=operation_name,
            connection=alias,
            backend=backend,
            phase=phase,
        )
        preview_line = _first_non_empty_sql_line(preview_sql)
        if preview_line is not None:
            time_print(
                f"Finished SQL statement:\n{preview_line}",
                operation=operation_name,
                connection=alias,
                backend=backend,
                phase=phase,
            )


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
    if retry_cnt < 1:
        raise ValueError("retry_cnt must be at least 1.")
    if timeout_increment < 0:
        raise ValueError("timeout_increment must be non-negative.")


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
        connection_ref: ConnectionRef = {"connection": open_connection(connection_key)}
        try:
            return operation(connection_ref, attempt)
        except Exception as exc:
            annotate_sql_exception(exc, context_factory(attempt))
            if backend == "gp":
                _rollback_quietly(connection_ref["connection"])
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
        f"Closing {connection_key} connection",
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
    from .dml.transfer.runtime.retry import run_with_retry

    return run_with_retry(**kwargs)


def _rollback_quietly(connection: Any) -> None:
    from .dml.transfer.runtime.retry import rollback_quietly

    rollback_quietly(connection)
