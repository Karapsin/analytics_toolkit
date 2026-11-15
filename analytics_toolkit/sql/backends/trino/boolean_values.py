"""Deliberate Trino BOOLEAN casts follow the native signed-BIGINT operator."""

from __future__ import annotations

from functools import lru_cache
from numbers import Integral
from typing import Any

import numpy as np

from .date_values import normalize_authoritative_date

_BIGINT_MIN = -(2**63)
_BIGINT_MAX = 2**63 - 1
_INVALID_VALUE = (
    "Trino BOOLEAN requires a native Boolean or signed BIGINT integer. "
    "For other backend conversions, CAST the source expression AS BOOLEAN in from_sql."
)
_INVALID_TYPE = "Trino BOOLEAN requires the unparameterized BOOLEAN type without wrapper syntax."


class TrinoBooleanValueError(ValueError):
    """An unsupported explicit Boolean cast cannot be repaired by replay."""

    analytics_toolkit_sql_retry_safe = False


@lru_cache(maxsize=1024)
def is_boolean_type(native_type: str | None) -> bool:
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar cycle.
        parse_native_type,
    )

    native = parse_native_type(native_type)
    if native.base.name != "boolean":
        return False
    if native.name != "boolean" or native.arguments or native.original.casefold() != "boolean":
        raise TrinoBooleanValueError(_INVALID_TYPE)
    return True


def boolean_value(value: Any) -> bool:
    """Match BigintOperators.castToBoolean without float or string truthiness."""
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, Integral):
        integer = int(value)
        if _BIGINT_MIN <= integer <= _BIGINT_MAX:
            return integer != 0
    raise TrinoBooleanValueError(_INVALID_VALUE)


@lru_cache(maxsize=1024)
def contains_boolean_type(native_type: str) -> bool:
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- recursive native grammar.
        nested_descriptor,
    )

    if is_boolean_type(native_type):
        return True
    children = [contains_boolean_type(child) for _name, child in nested_descriptor(native_type)[1]]
    return any(children)


def validate_boolean_type_profile(native_type: str) -> bool:
    """Check declaration syntax even when no value is present; admit no family."""
    contains_boolean_type(native_type)
    return False


def normalize_authoritative_boolean(value: Any, native_type: str | None) -> Any:
    """Normalize only authoritative Boolean views, retaining DATE dispatch."""
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- registry initialization.
        is_null,
    )

    if native_type is None or not contains_boolean_type(native_type):
        return normalize_authoritative_date(value, native_type)
    if is_null(value):
        return None
    if is_boolean_type(native_type):
        return boolean_value(value)
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- recursive codec cycle.
        ResolvedColumn,
    )

    # Existing nested conversion validates all MAP pairs before constructing a dict.
    return ResolvedColumn(native_type, target_backend="trino").convert(value)


__all__ = [
    "TrinoBooleanValueError",
    "boolean_value",
    "contains_boolean_type",
    "is_boolean_type",
    "normalize_authoritative_boolean",
    "validate_boolean_type_profile",
]
