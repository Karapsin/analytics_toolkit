from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from .retry_cleanup import cleanup_preserving_error

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def cursor_scope(connection: Any, *, context_manager: bool = False) -> Iterator[Any]:
    """Close a cursor without replacing an execution failure or replaying success."""
    manager = connection.cursor()
    cursor = manager.__enter__() if context_manager else manager
    error: BaseException | None = None
    try:
        yield cursor
    except BaseException as exc:
        error = exc
        raise
    finally:
        if context_manager:
            cleanup_preserving_error(
                lambda: manager.__exit__(
                    type(error) if error is not None else None,
                    error,
                    error.__traceback__ if error is not None else None,
                ),
                error=error,
                phase="close_cursor",
            )
        else:
            cleanup_preserving_error(
                lambda: _close_cursor(manager), error=error, phase="close_cursor"
            )


def _close_cursor(cursor: Any) -> None:
    cursor.close()
