"""Greenplum Boolean assignments use its native text input grammar."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import numpy as np

from .date_values import normalize_authoritative_date
from .type_identity import canonical_builtin_type

_ASCII_WHITESPACE = " \t\n\r\v\f"
_TEXT_VALUES = {
    "true": True,
    "yes": True,
    "on": True,
    "1": True,
    "false": False,
    "no": False,
    "off": False,
    "0": False,
}
_INVALID_BOOLEAN = (
    "Greenplum BOOLEAN requires a native Boolean or supported ASCII Boolean text. "
    "For other backend conversions, CAST the source expression AS BOOLEAN in from_sql."
)


class GreenplumBooleanValueError(ValueError):
    """Invalid Boolean text cannot be repaired by replaying its assignment."""

    analytics_toolkit_sql_retry_safe = False


@lru_cache(maxsize=1024)
def is_boolean_type(native_type: str | None) -> bool:
    return canonical_builtin_type(native_type or "") == "BOOLEAN"


def boolean_value(value: Any) -> bool | None:
    """Match boolin's unique-prefix grammar without generic Python truthiness."""
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- registry initialization.
        is_null,
    )

    if is_null(value):
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, str) and value.isascii():
        token = value.strip(_ASCII_WHITESPACE).lower()
        matches = {result for literal, result in _TEXT_VALUES.items() if literal.startswith(token)}
        if len(matches) == 1:
            return matches.pop()
    raise GreenplumBooleanValueError(_INVALID_BOOLEAN)


@lru_cache(maxsize=1024)
def contains_boolean_type(native_type: str) -> bool:
    """Recognize built-in arrays while leaving user-defined types to the server."""
    if is_boolean_type(native_type):
        return True
    canonical = canonical_builtin_type(native_type)
    if canonical is None:
        return False
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- recursive native grammar.
        nested_descriptor,
    )

    return any(contains_boolean_type(child) for _name, child in nested_descriptor(canonical)[1])


def normalize_authoritative_boolean(value: Any, native_type: str | None) -> Any:
    """Normalize only declared built-in Boolean views, retaining DATE dispatch."""
    if native_type is None or not contains_boolean_type(native_type):
        return normalize_authoritative_date(value, native_type)
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- registry initialization.
        is_null,
    )

    if is_null(value):
        return None
    if is_boolean_type(native_type):
        return boolean_value(value)
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- recursive codec cycle.
        ResolvedColumn,
    )

    return ResolvedColumn(native_type, target_backend="gp").convert(value)


__all__ = [
    "GreenplumBooleanValueError",
    "boolean_value",
    "contains_boolean_type",
    "is_boolean_type",
    "normalize_authoritative_boolean",
]
