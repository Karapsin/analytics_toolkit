"""Certify Greenplum integer text without changing other native scalar conversions."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from .boolean_values import normalize_authoritative_boolean
from .type_identity import canonical_builtin_type

_WIDTHS = {"SMALLINT": 16, "INTEGER": 32, "BIGINT": 64}
_ASCII_WHITESPACE = " \t\r\n\v\f"
_DECIMAL_INTEGER = re.compile(r"[+-]?[0-9]+", flags=re.ASCII)
_INVALID_INTEGER = (
    "Greenplum integer input requires ASCII signed decimal text within the declared "
    "SMALLINT, INTEGER or BIGINT range. For other conversions, CAST the source expression "
    "AS the destination integer type in from_sql."
)


class GreenplumIntegerValueError(ValueError):
    """Invalid or out-of-range integer text cannot be repaired by replay."""

    analytics_toolkit_sql_retry_safe = False


@lru_cache(maxsize=1024)
def is_integer_type(native_type: str | None) -> bool:
    return canonical_builtin_type(native_type or "") in _WIDTHS


def integer_text_value(value: str, native_type: str) -> int:
    """Match the native signed-decimal input grammar and exact destination width."""
    bits = _WIDTHS.get(canonical_builtin_type(native_type) or "")
    if bits is None or not isinstance(value, str) or not value.isascii():
        raise GreenplumIntegerValueError(_INVALID_INTEGER)
    token = value.strip(_ASCII_WHITESPACE)
    if _DECIMAL_INTEGER.fullmatch(token) is None:
        raise GreenplumIntegerValueError(_INVALID_INTEGER)
    negative = token[0] == "-"
    digits = token[1:] if token[0] in {"+", "-"} else token
    digits = digits.lstrip("0") or "0"
    limit = 2 ** (bits - 1)
    # Arbitrarily many leading zeros are native-valid. Only significant digits
    # reach Python's bounded decimal parser, avoiding its global digit limit.
    if len(digits) > len(str(limit)):
        raise GreenplumIntegerValueError(_INVALID_INTEGER)
    integer = -int(digits) if negative else int(digits)
    if not -limit <= integer <= limit - 1:
        raise GreenplumIntegerValueError(_INVALID_INTEGER)
    return integer


def integer_value(value: Any, native_type: str) -> Any:
    """Normalize only text; preserve existing numeric and integral-value paths."""
    return integer_text_value(value, native_type) if isinstance(value, str) else value


@lru_cache(maxsize=1024)
def contains_integer_type(native_type: str) -> bool:
    """Recognize built-in integer arrays while delegating custom types to the server."""
    if is_integer_type(native_type):
        return True
    canonical = canonical_builtin_type(native_type)
    if canonical is None:
        return False
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- recursive native grammar.
        nested_descriptor,
    )

    return any(contains_integer_type(child) for _name, child in nested_descriptor(canonical)[1])


def normalize_authoritative_integer(value: Any, native_type: str | None) -> Any:
    """Normalize declared integer views, retaining Boolean, DATE and domain dispatch."""
    if native_type is None or not contains_integer_type(native_type):
        return normalize_authoritative_boolean(value, native_type)
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- registry initialization.
        is_null,
    )

    if is_null(value):
        return None
    if is_integer_type(native_type):
        return integer_value(value, native_type)
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- recursive codec cycle.
        ResolvedColumn,
    )

    return ResolvedColumn(native_type, target_backend="gp").convert(value)


__all__ = [
    "GreenplumIntegerValueError",
    "contains_integer_type",
    "integer_text_value",
    "integer_value",
    "is_integer_type",
    "normalize_authoritative_integer",
]
