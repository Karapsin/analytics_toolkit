from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from analytics_toolkit.general import time_print

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


@contextmanager
def connection_scope(connection: Any, *, phase: str = "close_connection") -> Iterator[Any]:
    """Close a connection without changing the established operation outcome."""
    error: BaseException | None = None
    try:
        yield connection
    except BaseException as exc:
        error = exc
        raise
    finally:
        cleanup_preserving_error(connection.close, error=error, phase=phase)


def mark_sql_error(exc: BaseException, backend: str) -> None:
    """Retain the failing backend and terminal decisions across recovery wrappers."""
    exc.__dict__.setdefault("analytics_toolkit_sql_backend", backend)
    if isinstance(exc, Exception):
        # Runtime retry imports its runner; defer this import to avoid that cycle.
        from analytics_toolkit.sql.dml.transfer.runtime.retry import (  # noqa: PLC0415
            is_non_retryable_sql_error,
        )

        if is_non_retryable_sql_error(exc, backend=backend):
            exc.__dict__["analytics_toolkit_sql_retry_safe"] = False


def cleanup_preserving_error(
    cleanup: Callable[[], Any],
    *,
    error: BaseException | None,
    phase: str,
) -> None:
    """Cleanup cannot replace a primary error or replay an established outcome."""
    try:
        cleanup()
    except Exception as cleanup_error:  # noqa: BLE001 - driver cleanup cannot replace SQL outcomes.
        message = f"SQL {phase} also failed: {type(cleanup_error).__name__}"
        if error is not None:
            add_note = getattr(error, "add_note", None)
            if callable(add_note):
                add_note(message)
            else:
                notes = list(getattr(error, "__notes__", ()))
                notes.append(message)
                error.__dict__["__notes__"] = notes
        time_print(message, level="warning", phase=phase)
