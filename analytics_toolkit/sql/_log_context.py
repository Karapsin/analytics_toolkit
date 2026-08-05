from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from analytics_toolkit.general.logging import _time_print_message_prefix

_SQL_LOG_CONTEXT: ContextVar[tuple[str, bool]] = ContextVar(
    "analytics_toolkit_sql_log_context",
    default=("", False),
)


@contextmanager
def sql_log_context(prefix: str = "", *, suppress_sql: bool = False) -> Any:
    """Temporarily prefix nested SQL logs and optionally hide SQL text."""
    token = _SQL_LOG_CONTEXT.set((prefix, suppress_sql))
    try:
        with _time_print_message_prefix(prefix):
            yield
    finally:
        _SQL_LOG_CONTEXT.reset(token)


def current_sql_log_context() -> tuple[str, bool]:
    return _SQL_LOG_CONTEXT.get()


def prefix_sql_log_message(message: str) -> str:
    prefix, _suppress_sql = current_sql_log_context()
    if prefix and not message.startswith(prefix):
        return f"{prefix}{message}"
    return message
