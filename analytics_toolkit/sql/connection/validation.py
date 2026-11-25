"""Validation-only budgets and safe diagnostics, independent of ordinary retries."""

from __future__ import annotations

import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from typing import Any, Iterator

from .config_diagnostics import _error_source_path
from .errors import SqlConfigError, UnsupportedConnectionTypeError

VALIDATION_BUDGET_SECONDS = 5
_VALIDATING: ContextVar[bool] = ContextVar("sql_connectivity_validation", default=False)
_DEADLINE: ContextVar[float | None] = ContextVar("sql_validation_deadline", default=None)


@contextmanager
def validation_scope() -> Iterator[None]:
    if _VALIDATING.get():
        # Backend opening and probing may nest scopes; none may restart a probe.
        yield
        return
    token = _VALIDATING.set(True)
    started = time.monotonic()
    deadline_token = _DEADLINE.set(started + VALIDATION_BUDGET_SECONDS)
    try:
        yield
    finally:
        _DEADLINE.reset(deadline_token)
        _VALIDATING.reset(token)


def validation_timeout(configured: float | None) -> int | float | None:
    if not _VALIDATING.get():
        return configured
    remaining = validation_remaining()
    return (
        min(configured, remaining)
        if configured is not None and configured > 0 and remaining is not None
        else remaining
    )


def validation_remaining() -> float | None:
    """Share one probe deadline across connection opening, pages and progress."""
    deadline = _DEADLINE.get()
    if not _VALIDATING.get() or deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        message = "Connectivity probe exceeded its validation budget."
        raise TimeoutError(message)
    return remaining


def validating() -> bool:
    return _VALIDATING.get()


def close_validation_resource(resource: Any) -> None:
    # Validation reports connectivity, never driver cleanup internals or credentials.
    with suppress(Exception):
        resource.close()


def safe_validation_error(exc: Exception, *, connecting: bool) -> str:
    # Driver messages routinely embed DSNs, URLs, usernames and passwords. The
    # exception class gives a useful diagnostic without reflecting private values.
    if (
        not connecting
        and isinstance(exc, (SqlConfigError, UnsupportedConnectionTypeError))
        and exc.__cause__ is None
        and exc.__context__ is None
    ):
        return f"{type(exc).__name__}: {exc}"
    phase = "Connectivity probe" if connecting else "Configuration validation"
    message = f"{phase} failed ({type(exc).__name__})."
    if isinstance(exc, (SqlConfigError, UnsupportedConnectionTypeError)):
        with suppress(Exception):
            path = _error_source_path()
            if path is not None:
                message += f" [SQL connections file: {path}]"
    return message
