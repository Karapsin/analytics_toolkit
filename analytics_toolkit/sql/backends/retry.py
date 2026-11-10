from __future__ import annotations

import re
from typing import TYPE_CHECKING, Tuple, Union

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

from .ch.retry import TERMINAL_ERROR_CODES
from .ch.retry import classify_error as classify_ch_error
from .gp.retry import classify_error as classify_gp_error
from .trino.retry import NON_RETRYABLE_ERROR_REASONS
from .trino.retry import classify_error as classify_trino_error

PolicyResult = Tuple[bool, str, Union[str, int, None]]
_CLASSIFIERS: dict[str, Callable[[BaseException], PolicyResult | None]] = {
    "ch": classify_ch_error,
    "gp": classify_gp_error,
    "trino": classify_trino_error,
}
_CLICKHOUSE_DIAGNOSTIC = re.compile(
    r"^(?:Received ClickHouse exception\b|(?:Code:\s*\d+\.\s*)?DB::Exception:"
    r"|:?HTTPDriver for \S+ returned response code \d+\)"
    r"|HTTP driver received HTTP status \d{3}\b)",
    re.IGNORECASE,
)
_CLICKHOUSE_TERMINAL_NAMES = frozenset(name for name, _reason in TERMINAL_ERROR_CODES.values())
_DRIVER_MODULES = {
    "clickhouse_driver.": "ch",
    "clickhouse_connect.": "ch",
    "trino.": "trino",
    "psycopg2": "gp",
}


def classify_backend_error(
    exc: BaseException, *, backend: str | None = None
) -> tuple[str, PolicyResult] | None:
    """Dispatch by the failing engine without opening connections or resolving aliases."""
    # Native SQLSTATE/class evidence must win over a transfer's destination hint.
    gp_result = classify_gp_error(exc)
    if gp_result is not None:
        return "gp", gp_result
    origin = _native_backend(exc) or _backend_hint(exc, backend)
    if origin is None:
        return None
    result = _CLASSIFIERS[origin](exc)
    return (origin, result) if result is not None else None


def _native_backend(exc: BaseException) -> str | None:
    origin = next(
        (
            backend
            for cls in type(exc).__mro__
            for prefix, backend in _DRIVER_MODULES.items()
            if cls.__module__.startswith(prefix)
        ),
        None,
    )
    if origin is None:
        if _CLICKHOUSE_DIAGNOSTIC.match(str(exc).lstrip()):
            origin = "ch"
        elif getattr(exc, "error_type", None):
            origin = "trino"
        else:
            origin = _symbolic_backend(exc)
    return origin


def _symbolic_backend(exc: BaseException) -> str | None:
    error_name = getattr(exc, "error_name", None)
    name = error_name or getattr(exc, "name", None)
    name = name.strip().upper() if isinstance(name, str) else None
    if name in NON_RETRYABLE_ERROR_REASONS:
        return "trino"
    if name in _CLICKHOUSE_TERMINAL_NAMES:
        return "ch"
    return "trino" if error_name else None


def _backend_hint(exc: BaseException, backend: str | None) -> str | None:
    context = getattr(exc, "sql_context", None)
    candidates: tuple[Any, ...] = (
        getattr(exc, "analytics_toolkit_sql_backend", None),
        getattr(exc, "backend", None),
        backend,
        getattr(context, "backend", None),
    )
    return next(
        (item for item in candidates if isinstance(item, str) and item in _CLASSIFIERS), None
    )
