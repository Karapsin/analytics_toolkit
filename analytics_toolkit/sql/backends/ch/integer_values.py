"""Deliberate ClickHouse integer assignments preserve exact Python casts."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from .boolean_values import normalize_authoritative_boolean
from .storage_types import logical_storage_type

_INTEGER_NAME = re.compile(r"(u?)int(8|16|32|64|128|256)")
_INVALID_TYPE = "ClickHouse integer values require an unparameterized native Int or UInt type."
_INVALID_VALUE = (
    "Selected input is incompatible with authoritative integer schema or outside "
    "the authoritative integer range (ClickHouse). "
    "For other backend conversions, CAST the source expression to the required integer type "
    "in from_sql."
)


class ClickHouseIntegerValueError(ValueError):
    """Invalid integer input cannot be repaired by replaying its assignment."""

    analytics_toolkit_sql_retry_safe = False


@lru_cache(maxsize=1024)
def _integer_domain(native_type: str | None) -> tuple[int, int] | None:
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar cycle.
        parse_native_type,
    )

    native = parse_native_type(logical_storage_type(native_type or "")).base
    match = _INTEGER_NAME.fullmatch(native.name)
    if match is None:
        return None
    if native.arguments or native.original.casefold() != native.name:
        raise ClickHouseIntegerValueError(_INVALID_TYPE)
    bits = int(match.group(2))
    return (0, 2**bits - 1) if match.group(1) else (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)


def is_integer_type(native_type: str | None) -> bool:
    return _integer_domain(native_type) is not None


def integer_value(value: Any, native_type: str) -> int | None:
    """Apply the historical int(value), then enforce the exact native domain."""
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- registry initialization.
        is_null,
    )

    domain = _integer_domain(native_type)
    if domain is None:
        raise ClickHouseIntegerValueError(_INVALID_TYPE)
    if is_null(value):
        return None
    try:
        converted = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ClickHouseIntegerValueError(_INVALID_VALUE) from None
    if not domain[0] <= converted <= domain[1]:
        raise ClickHouseIntegerValueError(_INVALID_VALUE)
    return converted


@lru_cache(maxsize=1024)
def contains_integer_type(native_type: str) -> bool:
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- recursive native grammar.
        nested_descriptor,
    )

    if is_integer_type(native_type):
        return True
    logical = logical_storage_type(native_type)
    children = [contains_integer_type(child) for _name, child in nested_descriptor(logical)[1]]
    return any(children)


def validate_integer_type_profile(native_type: str) -> bool:
    """Check declarations without admitting raw text or inventing domain proofs."""
    contains_integer_type(native_type)
    return False


def normalize_authoritative_integer(value: Any, native_type: str | None) -> Any:
    """Use converted authoritative views while retaining raw caller/spool input."""
    if native_type is None or not contains_integer_type(native_type):
        return normalize_authoritative_boolean(value, native_type)
    if is_integer_type(native_type):
        return integer_value(value, native_type)
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- registry initialization.
        is_null,
    )

    if is_null(value):
        return None
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- recursive codec cycle.
        ResolvedColumn,
    )

    # The shared CH codec retains ordered MAP pairs even when converted keys repeat.
    return ResolvedColumn(native_type, target_backend="ch").convert(value)


__all__ = [
    "ClickHouseIntegerValueError",
    "contains_integer_type",
    "integer_value",
    "is_integer_type",
    "normalize_authoritative_integer",
    "validate_integer_type_profile",
]
