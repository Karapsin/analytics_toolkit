"""Deliberate Trino integer assignments retain their native conversion paths."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from numbers import Integral
from typing import Any
from unicodedata import decimal

import numpy as np

from .boolean_values import normalize_authoritative_boolean

_INTEGER_BITS = {"tinyint": 8, "smallint": 16, "integer": 32, "int": 32, "bigint": 64}
_JAVA_TRIM_CHARACTERS = "".join(chr(codepoint) for codepoint in range(33))
_JAVA_CHAR_MAX = 0xFFFF
_INVALID_TYPE = (
    "Trino integer assignments require an unparameterized native integer type without wrappers."
)
_INVALID_VALUE = (
    "Selected input is incompatible with authoritative integer schema or outside "
    "the authoritative integer range (Trino). "
    "For other backend conversions, CAST the source expression to the required integer type "
    "in from_sql."
)


class TrinoIntegerValueError(ValueError):
    """Invalid integer input cannot be repaired by replaying its assignment."""

    analytics_toolkit_sql_retry_safe = False


@lru_cache(maxsize=1024)
def is_integer_type(native_type: str | None) -> bool:
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar cycle.
        parse_native_type,
    )

    native = parse_native_type(native_type)
    if native.base.name not in _INTEGER_BITS:
        return False
    if (
        native.name != native.base.name
        or native.arguments
        or native.original.casefold() != native.name
    ):
        raise TrinoIntegerValueError(_INVALID_TYPE)
    return True


def _varchar_integer(value: str, minimum: int, maximum: int) -> int:
    # NumberParser.parseTrimmedLong delegates non-ASCII digits to Java's char parser.
    token = value.strip(_JAVA_TRIM_CHARACTERS)
    negative = token.startswith("-")
    digits = token[1:] if token.startswith(("+", "-")) else token
    if not digits:
        raise TrinoIntegerValueError(_INVALID_VALUE)
    limit = -minimum if negative else maximum
    result = 0
    for character in digits:
        if ord(character) > _JAVA_CHAR_MAX:
            raise TrinoIntegerValueError(_INVALID_VALUE)
        result = result * 10 + decimal(character)
        if result > limit:
            raise TrinoIntegerValueError(_INVALID_VALUE)
    return -result if negative else result


def integer_text_value(value: str, native_type: str) -> int:
    """Validate SQL VARCHAR casts, including BIGINT's native text grammar."""
    if not is_integer_type(native_type):
        raise TrinoIntegerValueError(_INVALID_TYPE)
    if not isinstance(value, str):
        raise TrinoIntegerValueError(_INVALID_VALUE)
    bits = _INTEGER_BITS[native_type.strip().casefold()]
    minimum, maximum = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
    try:
        return _varchar_integer(value, minimum, maximum)
    except (TypeError, ValueError, OverflowError):
        raise TrinoIntegerValueError(_INVALID_VALUE) from None


def integer_value(value: Any, native_type: str) -> int:
    """Keep historical BIGINT int(value); parse sibling text as Trino does."""
    if not is_integer_type(native_type):
        raise TrinoIntegerValueError(_INVALID_TYPE)
    native = native_type.strip().casefold()
    bits = _INTEGER_BITS[native]
    minimum, maximum = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
    try:
        if native == "bigint":
            result = int(value)
        elif isinstance(value, Decimal):
            if not value.is_finite() or value != value.to_integral_value():
                raise TrinoIntegerValueError(_INVALID_VALUE)
            result = int(value)
        elif isinstance(value, (Integral, bool, np.bool_)):
            result = int(value)
        elif isinstance(value, str):
            result = integer_text_value(value, native_type)
        else:
            raise TrinoIntegerValueError(_INVALID_VALUE)
    except (TypeError, ValueError, OverflowError):
        raise TrinoIntegerValueError(_INVALID_VALUE) from None
    if not minimum <= result <= maximum:
        raise TrinoIntegerValueError(_INVALID_VALUE)
    return result


def convert_integer_value(value: Any, native_type: str) -> tuple[bool, Any]:
    """Leave sibling non-text families on their existing generic conversion path."""
    if not is_integer_type(native_type):
        return False, value
    if native_type.strip().casefold() == "bigint" or isinstance(
        value, (str, Integral, Decimal, bool, np.bool_)
    ):
        return True, integer_value(value, native_type)
    return False, value


@lru_cache(maxsize=1024)
def contains_integer_type(native_type: str) -> bool:
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- recursive native grammar.
        nested_descriptor,
    )

    if is_integer_type(native_type):
        return True
    children = [contains_integer_type(child) for _name, child in nested_descriptor(native_type)[1]]
    return any(children)


def validate_integer_type_profile(native_type: str) -> bool:
    """Check declarations even for empty input without admitting new families."""
    contains_integer_type(native_type)
    return False


def normalize_authoritative_integer(value: Any, native_type: str | None) -> Any:
    """Normalize authoritative integer views while retaining Boolean/DATE dispatch."""
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- registry initialization.
        is_null,
    )

    if native_type is None or not contains_integer_type(native_type):
        return normalize_authoritative_boolean(value, native_type)
    if is_null(value):
        return None
    if is_integer_type(native_type):
        # The dataframe seam retains its existing exact integral-Real conversion.
        return convert_integer_value(value, native_type)[1]
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- recursive codec cycle.
        ResolvedColumn,
    )

    # Existing MAP conversion validates converted pairs before constructing a dict.
    return ResolvedColumn(native_type, target_backend="trino").convert(value)


__all__ = [
    "TrinoIntegerValueError",
    "contains_integer_type",
    "convert_integer_value",
    "integer_text_value",
    "integer_value",
    "is_integer_type",
    "normalize_authoritative_integer",
    "validate_integer_type_profile",
]
