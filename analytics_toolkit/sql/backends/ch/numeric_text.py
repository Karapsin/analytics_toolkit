"""Finite ClickHouse numeric text views without changing numeric payloads."""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, cast

from .storage_types import logical_storage_type

_DECIMAL_NAMES = {"decimal", "decimal32", "decimal64", "decimal128", "decimal256"}
_DECIMAL_STORAGE_PRECISIONS = (9, 18, 38, 76)
_FLOAT64_MAX_DECIMAL_EXPONENT = 308
_INVALID_VALUE = (
    "ClickHouse numeric text requires a finite value within the declared precision, scale "
    "and range. For other backend numeric syntax, CAST the source expression in from_sql."
)
_NATIVE_TEXT = re.compile(
    r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE](?P<exponent>[+-]?[0-9]{1,3}))?", re.ASCII
)
_INVALID_NATIVE_TEXT = (
    "ClickHouse SQL numeric casts require canonical finite ASCII decimal text with an "
    "optional decimal exponent of at most three digits. For other numeric syntax, "
    "CAST the source expression in from_sql."
)


class ClickHouseNumericTextValueError(ValueError):
    """Invalid numeric text cannot be repaired by replaying its assignment."""

    analytics_toolkit_sql_retry_safe = False


@lru_cache(maxsize=1024)
def _numeric_kind(native_type: str | None) -> str | None:
    from analytics_toolkit.sql.backends.decimal_values import (  # noqa: PLC0415 -- native grammar cycle.
        decimal_target_type,
    )
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar cycle.
        _FLOAT_TYPES,
        parse_native_type,
    )

    logical = decimal_target_type(logical_storage_type(native_type or ""), "ch")
    native = parse_native_type(logical).base
    if native.name in _DECIMAL_NAMES:
        return "decimal"
    return "float" if native.name in _FLOAT_TYPES else None


def is_numeric_text_type(native_type: str | None) -> bool:
    """Identify scalar numeric value types, retaining their original SQL spelling."""
    return _numeric_kind(native_type) is not None


@lru_cache(maxsize=1024)
def contains_numeric_text_type(native_type: str) -> bool:
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- recursive native grammar.
        nested_descriptor,
    )

    if is_numeric_text_type(native_type):
        return True
    children = [
        contains_numeric_text_type(child)
        for _name, child in nested_descriptor(logical_storage_type(native_type))[1]
    ]
    return any(children)


def validate_numeric_text_type_profile(native_type: str) -> bool:
    """Check declarations independently of values without admitting source families."""
    from analytics_toolkit.sql.backends.decimal_values import (  # noqa: PLC0415 -- native grammar cycle.
        validate_decimal_type,
    )

    contains_numeric_text_type(native_type)
    validate_decimal_type(logical_storage_type(native_type), "ch")
    return False


def _validate_numeric(value: Decimal | float, native_type: str, kind: str) -> Decimal | float:
    if kind == "decimal":
        from analytics_toolkit.sql.backends.decimal_values import (  # noqa: PLC0415 -- exact native domain.
            convert_decimal_value,
        )

        return cast("Decimal", convert_decimal_value(value, native_type, "ch")[1])
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native profile cycle.
        ColumnProfile,
        validate_profile,
    )

    profile = ColumnProfile()
    profile.add(value)
    validate_profile(profile, native_type, "ch")
    return value


def convert_numeric_text(value: Any, native_type: str | None) -> tuple[bool, Any]:
    """Convert declared finite numeric text; leave all other scalar paths alone."""
    if not isinstance(value, str):
        return False, value
    kind = _numeric_kind(native_type)
    if kind is None:
        return False, value
    try:
        parsed = Decimal(str(value)) if kind == "decimal" else float(value)
    except (InvalidOperation, ValueError, OverflowError):
        raise ClickHouseNumericTextValueError(_INVALID_VALUE) from None
    finite = parsed.is_finite() if isinstance(parsed, Decimal) else math.isfinite(parsed)
    if not finite:
        raise ClickHouseNumericTextValueError(_INVALID_VALUE)
    try:
        converted = _validate_numeric(parsed, logical_storage_type(native_type or ""), kind)
    except (InvalidOperation, ValueError, OverflowError):
        raise ClickHouseNumericTextValueError(_INVALID_VALUE) from None
    return True, converted


def _native_cast_numeric(exact: Decimal, logical: str, kind: str, exponent: int) -> Decimal | float:
    if kind == "decimal":
        from analytics_toolkit.sql.backends.decimal_values import (  # noqa: PLC0415 -- declared decimal shape.
            decimal_target_type,
        )
        from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native domain cycle.
            decimal_domain,
        )

        shape = decimal_domain(decimal_target_type(logical, "ch"))
        if exact.is_zero() and shape is not None:
            precision = next(
                (limit for limit in _DECIMAL_STORAGE_PRECISIONS if shape[0] <= limit), shape[0]
            )
            if exponent > precision - shape[1]:
                raise ClickHouseNumericTextValueError(_INVALID_NATIVE_TEXT)
        return _validate_numeric(exact, logical, kind)
    from analytics_toolkit.sql.backends.finite_numeric_text import (  # noqa: PLC0415 -- shared IEEE math.
        decimal_float_value,
    )
    from analytics_toolkit.sql.backends.native_values import (  # noqa: PLC0415 -- shared native width.
        _float_width,
    )

    if exact.is_zero() and exponent > _FLOAT64_MAX_DECIMAL_EXPONENT:
        raise ClickHouseNumericTextValueError(_INVALID_NATIVE_TEXT)
    bits = cast("int", _float_width(logical, "ch"))
    parsed = decimal_float_value(exact, bits, reject_underflow=True)
    return _validate_numeric(parsed, logical, kind)


def native_text_cast_value(value: Any, native_type: str) -> Decimal | float | None:
    """Validate a canonical SQL text-cast subset without rewriting stage strings."""
    if value is None:
        return None
    kind = _numeric_kind(native_type)
    match = _NATIVE_TEXT.fullmatch(value) if isinstance(value, str) else None
    if kind is None or match is None:
        raise ClickHouseNumericTextValueError(_INVALID_NATIVE_TEXT)
    exact = Decimal(value)
    exponent = int(match.group("exponent") or "0")
    try:
        converted = _native_cast_numeric(exact, logical_storage_type(native_type), kind, exponent)
    except (InvalidOperation, ValueError, OverflowError):
        raise ClickHouseNumericTextValueError(_INVALID_NATIVE_TEXT) from None
    return converted


def normalize_authoritative_numeric_text(value: Any, native_type: str | None) -> Any:
    """Produce a validation view without changing inference or caller data."""
    if native_type is None or not contains_numeric_text_type(native_type):
        return value
    if is_numeric_text_type(native_type):
        return convert_numeric_text(value, native_type)[1]
    from analytics_toolkit.sql.backends.numeric_text_views import (  # noqa: PLC0415 -- text-only container view.
        numeric_text_view,
    )

    return numeric_text_view(value, native_type, convert_numeric_text, logical_storage_type)


def _authoritative_scalar(value: Any, native_type: str) -> tuple[bool, Any]:
    handled, converted = convert_numeric_text(value, native_type)
    if handled:
        return True, converted
    from .integer_values import (  # noqa: PLC0415 -- scalar native preprocessing only.
        normalize_authoritative_integer,
    )

    return True, normalize_authoritative_integer(value, native_type)


def normalize_authoritative_value(value: Any, native_type: str | None) -> Any:
    """Compose native scalar views without eagerly converting mixed containers."""
    if native_type is None:
        return value
    from analytics_toolkit.sql.backends.numeric_text_views import (  # noqa: PLC0415 -- one-pass scalar view.
        numeric_text_view,
    )

    return numeric_text_view(value, native_type, _authoritative_scalar, logical_storage_type)


__all__ = [
    "ClickHouseNumericTextValueError",
    "contains_numeric_text_type",
    "convert_numeric_text",
    "is_numeric_text_type",
    "native_text_cast_value",
    "normalize_authoritative_numeric_text",
    "normalize_authoritative_value",
    "validate_numeric_text_type_profile",
]
