"""Explicit ClickHouse Bool views match both supported numeric wire encoders."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from numbers import Integral
from typing import Any

import numpy as np

from .storage_types import logical_storage_type

_BOOLEAN_NAMES = {"bool", "boolean"}
_INVALID_VALUE = "ClickHouse Bool requires a native Boolean or supported numeric scalar."
_INVALID_TYPE = "ClickHouse Bool requires an unparameterized Bool or Boolean value type."


class ClickHouseBooleanValueError(ValueError):
    """Unsupported Bool input cannot be repaired by replaying its assignment."""

    analytics_toolkit_sql_retry_safe = False


@lru_cache(maxsize=1024)
def is_boolean_type(native_type: str | None) -> bool:
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar cycle.
        parse_native_type,
    )

    native = parse_native_type(logical_storage_type(native_type or "")).base
    if native.name not in _BOOLEAN_NAMES:
        return False
    if native.arguments or native.original.casefold() not in _BOOLEAN_NAMES:
        raise ClickHouseBooleanValueError(_INVALID_TYPE)
    return True


def boolean_value(value: Any) -> bool | None:
    """Use numeric zero/nonzero without width bounds or arbitrary truthiness."""
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- registry initialization.
        is_null,
    )

    if is_null(value):
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (Integral, float, np.float32, np.float64)):
        return bool(value != 0)
    if isinstance(value, Decimal):
        return not value.is_zero()
    raise ClickHouseBooleanValueError(_INVALID_VALUE)


@lru_cache(maxsize=1024)
def contains_boolean_type(native_type: str) -> bool:
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- recursive native grammar.
        nested_descriptor,
    )

    if is_boolean_type(native_type):
        return True
    logical = logical_storage_type(native_type)
    children = [contains_boolean_type(child) for _name, child in nested_descriptor(logical)[1]]
    return any(children)


def validate_boolean_type_profile(native_type: str) -> bool:
    """Check Bool declarations even without rows, admitting no source family."""
    contains_boolean_type(native_type)
    return False


def normalize_authoritative_boolean(value: Any, native_type: str | None) -> Any:
    """Normalize only a declared Bool-containing view, preserving raw input."""
    if native_type is None or not contains_boolean_type(native_type):
        return value
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

    # Existing CH Map conversion retains ordered pairs when converted keys repeat.
    return ResolvedColumn(native_type, target_backend="ch").convert(value)


__all__ = [
    "ClickHouseBooleanValueError",
    "boolean_value",
    "contains_boolean_type",
    "is_boolean_type",
    "normalize_authoritative_boolean",
    "validate_boolean_type_profile",
]
