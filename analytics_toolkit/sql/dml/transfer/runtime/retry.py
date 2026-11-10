from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection
from analytics_toolkit.sql.execution.cancellation import (
    current_cancellation_scope,
    raise_if_cancelled,
    wait_or_raise_if_cancelled,
)
from analytics_toolkit.sql.execution.operation_runner import _format_duration
from analytics_toolkit.sql.execution.retry_errors import classify_sql_error

T = TypeVar("T")
_MAX_SAFE_EXCEPTION_TYPE_CHARS = 80


def run_with_retry(  # noqa: PLR0913
    operation_name: str,
    retry_cnt: int,
    timeout_increment: float,
    operation: Callable[[int], Any],
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    non_retryable_predicate: Callable[[Exception], bool] | None = None,
    *,
    log_prefix: str = "",
    safe_exception_logging: bool = False,
    retry_status: Callable[[int, int], str] | None = None,
) -> Any:
    last_error: Exception | None = None

    for attempt in range(1, retry_cnt + 1):
        raise_if_cancelled()
        try:
            return operation(attempt)
        except Exception as exc:
            if not isinstance(exc, retryable_exceptions):
                raise
            decision = classify_sql_error(exc)
            stop = (
                non_retryable_predicate(exc)
                if non_retryable_predicate is not None
                else not decision.retryable
            )
            if stop:
                reason = "custom_predicate" if non_retryable_predicate else decision.reason
                code = f"; code={decision.code}" if decision.code is not None else ""
                time_print(
                    f"{log_prefix}Failed with a non-retryable error "
                    f"(reason={reason}{code}): "
                    f"{_logged_exception(exc, safe=safe_exception_logging)}",
                    level="warning",
                    operation=operation_name,
                    phase="retry",
                )
                raise
            last_error = exc
            if attempt >= retry_cnt:
                time_print(
                    f"{log_prefix}Failed after {attempt} attempt(s): "
                    f"{_logged_exception(exc, safe=safe_exception_logging)}",
                    level="warning",
                    operation=operation_name,
                    phase="retry",
                )
                break

            sleep_seconds = attempt * timeout_increment
            time_print(
                f"{log_prefix}Failed on attempt {attempt}/{retry_cnt}: "
                f"{_logged_exception(exc, safe=safe_exception_logging)}",
                level="warning",
                operation=operation_name,
                phase="retry",
            )
            retry_message = (
                retry_status(attempt + 1, retry_cnt)
                if retry_status is not None
                else f"Retrying in {_format_duration(sleep_seconds)}"
            )
            time_print(
                f"{log_prefix}{retry_message}",
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


def _logged_exception(exc: Exception, *, safe: bool) -> str:
    if not safe:
        return repr(exc)
    exception_type = type(exc).__name__
    if len(exception_type) > _MAX_SAFE_EXCEPTION_TYPE_CHARS:
        return exception_type[: _MAX_SAFE_EXCEPTION_TYPE_CHARS - 3] + "..."
    return exception_type


def is_non_retryable_sql_error(exc: Exception, *, backend: str | None = None) -> bool:
    """Compatibility entrypoint for recognized terminal SQL and setup failures."""
    return not classify_sql_error(exc, backend=backend).retryable


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


def close_connection_refs_preserving(
    error: BaseException | None,
    *connections: tuple[dict[str, Any], str, str],
) -> None:
    """Attempt every close while preserving and de-retrying an earlier failure."""
    failures: list[BaseException] = []
    for connection_ref, connection_type, role in connections:
        try:
            close_connection_ref(connection_ref, connection_type, role)
        except BaseException as exc:  # noqa: BLE001 -- cleanup must preserve interrupts too
            failures.append(exc)
            time_print("Failed", level="warning", connection=connection_type, phase=f"close_{role}")
    if not failures:
        return
    if error is None:
        raise failures[0].with_traceback(failures[0].__traceback__)
    try:
        error.__dict__["analytics_toolkit_sql_retry_safe"] = False
        add_note = getattr(error, "add_note", None)
        if callable(add_note):
            add_note(f"Connection cleanup also failed: {type(failures[0]).__name__}")
    except (AttributeError, TypeError):
        raise failures[0] from error
