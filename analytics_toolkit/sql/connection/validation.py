"""Validation-only budgets and safe diagnostics, independent of ordinary retries."""

from __future__ import annotations

from contextlib import contextmanager, suppress
from contextvars import ContextVar
from typing import Any, Iterator

from .errors import SqlConfigError, UnsupportedConnectionTypeError

VALIDATION_BUDGET_SECONDS = 5
_VALIDATING: ContextVar[bool] = ContextVar("sql_connectivity_validation", default=False)


@contextmanager
def validation_scope() -> Iterator[None]:
    token = _VALIDATING.set(True)
    try:
        yield
    finally:
        _VALIDATING.reset(token)


def validation_timeout(configured: float | None) -> int | float | None:
    if not _VALIDATING.get():
        return configured
    return min(configured, VALIDATION_BUDGET_SECONDS) if configured else VALIDATION_BUDGET_SECONDS


def validating() -> bool:
    return _VALIDATING.get()


def close_validation_resource(resource: Any) -> None:
    # Validation reports connectivity, never driver cleanup internals or credentials.
    with suppress(Exception):
        resource.close()


def safe_validation_error(exc: Exception, *, connecting: bool) -> str:
    # Driver messages routinely embed DSNs, URLs, usernames and passwords. The
    # exception class gives a useful diagnostic without reflecting private values.
    if not connecting and isinstance(exc, (SqlConfigError, UnsupportedConnectionTypeError)):
        return f"{type(exc).__name__}: {exc}"
    phase = "Connectivity probe" if connecting else "Configuration validation"
    return f"{phase} failed ({type(exc).__name__})."
