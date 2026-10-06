from __future__ import annotations

from contextlib import contextmanager, suppress
from contextvars import ContextVar
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar, cast

from analytics_toolkit.general import time_print
from analytics_toolkit.general.connections import (
    get_connections_path_override,
    get_connections_path_revision,
    get_last_connections_path,
)

from .config_cache import get_cached_connections_path
from .errors import SqlConfigError, UnsupportedConnectionTypeError, _add_exception_note

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

T = TypeVar("T")
_SOURCE_PATH: ContextVar[tuple[int, Path] | None] = ContextVar(
    "analytics_toolkit_sql_connections_error_source", default=None
)
_IN_OPERATION: ContextVar[bool] = ContextVar(
    "analytics_toolkit_sql_connections_error_operation", default=False
)
_FILE_SOURCE_ENABLED: ContextVar[bool] = ContextVar(
    "analytics_toolkit_sql_connections_file_source_enabled", default=True
)


def remember_error_source(path: Path) -> None:
    _SOURCE_PATH.set((get_connections_path_revision(), path))


@contextmanager
def sql_error_source_scope() -> Iterator[None]:
    """Keep provenance scoped to an operation, propagating nested source lookups."""
    nested = _IN_OPERATION.get()
    operation_token = _IN_OPERATION.set(True)
    path_token = _SOURCE_PATH.set(_SOURCE_PATH.get() if nested else None)
    try:
        yield
    finally:
        selected = _SOURCE_PATH.get()
        _SOURCE_PATH.reset(path_token)
        _IN_OPERATION.reset(operation_token)
        if nested and selected is not None:
            _SOURCE_PATH.set(selected)


@contextmanager
def fileless_connection_source() -> Iterator[None]:
    """Suppress unrelated file provenance during programmatic Airflow mode."""
    enabled_token = _FILE_SOURCE_ENABLED.set(False)
    path_token = _SOURCE_PATH.set(None)
    try:
        yield
    finally:
        _SOURCE_PATH.reset(path_token)
        _FILE_SOURCE_ENABLED.reset(enabled_token)


def _error_source_path() -> Path | None:
    if not _FILE_SOURCE_ENABLED.get():
        return None
    selected = _SOURCE_PATH.get()
    if selected is not None and (
        _IN_OPERATION.get() or selected[0] == get_connections_path_revision()
    ):
        return selected[1]
    return (
        get_cached_connections_path()
        or get_connections_path_override()
        or get_last_connections_path()
    )


def annotate_connections_exception(exc: Exception, *, configuration: bool = False) -> None:
    """Expose only source provenance; preserve the original exception and retry flags."""
    # A custom exception or logging sink must not replace the original failure.
    with suppress(Exception):
        _annotate_connections_exception(exc, configuration=configuration)


def _annotate_connections_exception(exc: Exception, *, configuration: bool) -> None:
    path = _error_source_path()
    if path is None:
        return
    note = f"SQL connections file: {path}"
    if configuration and isinstance(exc, (SqlConfigError, UnsupportedConnectionTypeError)):
        if str(path) not in str(exc):
            exc.args = (f"{exc} [{note}]",)
    elif str(path) not in str(exc) and note not in getattr(exc, "__notes__", ()):
        # Driver-defined exception classes may reject traceback notes.
        with suppress(Exception):
            _add_exception_note(exc, note)
    if not getattr(exc, "_sql_connections_path_logged", False):
        time_print(note, level="error", phase="error")
        vars(exc)["_sql_connections_path_logged"] = True


def connection_config_diagnostics(function: Callable[..., T]) -> Callable[..., T]:
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return function(*args, **kwargs)
        except Exception as exc:
            annotate_connections_exception(exc, configuration=True)
            raise

    return cast("Callable[..., T]", wrapper)
