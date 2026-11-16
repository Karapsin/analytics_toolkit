"""Finite Greenplum numeric text uses native input grammar before certification."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, cast

from analytics_toolkit.sql.backends.finite_numeric_text import decimal_float_value
from analytics_toolkit.sql.backends.gp.type_identity import canonical_builtin_type

_ASCII_WHITESPACE = " \t\r\n\v\f"
_FINITE_DECIMAL = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?",
    flags=re.ASCII,
)
_NUMERIC_TYPE = re.compile(r"NUMERIC(?:\([0-9]+, [0-9]+\))?", flags=re.ASCII)
_INVALID_NUMERIC_TEXT = (
    "Greenplum numeric input requires finite ASCII decimal text within the declared "
    "numeric or floating-point domain. For other source syntax, CAST the source "
    "expression AS the destination numeric type in from_sql."
)


class GreenplumNumericTextValueError(ValueError):
    """Unsupported numeric text cannot be repaired by replaying its assignment."""

    analytics_toolkit_sql_retry_safe = False


@lru_cache(maxsize=1024)
def _numeric_kind(native_type: str) -> str | None:
    canonical = canonical_builtin_type(native_type)
    if canonical == "REAL":
        return "float32"
    if canonical == "DOUBLE PRECISION":
        return "float64"
    return "decimal" if _NUMERIC_TYPE.fullmatch(canonical or "") else None


def is_numeric_text_type(native_type: str | None) -> bool:
    return _numeric_kind(native_type or "") is not None


def numeric_text_value(value: str, native_type: str) -> Decimal | float:
    """Parse the finite decimal input shared by quoted constants and explicit casts."""
    kind = _numeric_kind(native_type)
    if kind is None or not isinstance(value, str) or not value.isascii():
        raise GreenplumNumericTextValueError(_INVALID_NUMERIC_TEXT)
    token = value.strip(_ASCII_WHITESPACE)
    if _FINITE_DECIMAL.fullmatch(token) is None:
        raise GreenplumNumericTextValueError(_INVALID_NUMERIC_TEXT)
    try:
        numeric = Decimal(token)
    except (InvalidOperation, ValueError):
        raise GreenplumNumericTextValueError(_INVALID_NUMERIC_TEXT) from None
    if not numeric.is_finite():
        raise GreenplumNumericTextValueError(_INVALID_NUMERIC_TEXT)
    if kind == "decimal":
        from analytics_toolkit.sql.backends.decimal_values import (  # noqa: PLC0415 -- registry initialization.
            convert_decimal_value,
        )

        try:
            _handled, converted = convert_decimal_value(
                numeric, canonical_builtin_type(native_type) or native_type, "gp"
            )
        except ValueError:
            raise GreenplumNumericTextValueError(_INVALID_NUMERIC_TEXT) from None
        return cast("Decimal", converted)
    try:
        result = decimal_float_value(
            numeric, 32 if kind == "float32" else 64, reject_underflow=True
        )
    except ValueError:
        raise GreenplumNumericTextValueError(_INVALID_NUMERIC_TEXT) from None
    return result


def convert_numeric_text(value: Any, native_type: str) -> tuple[bool, Any]:
    """Leave non-text values and schemas outside the built-in numeric families untouched."""
    if not isinstance(value, str) or not is_numeric_text_type(native_type):
        return False, value
    return True, numeric_text_value(value, native_type)


@lru_cache(maxsize=1024)
def contains_numeric_text_type(native_type: str) -> bool:
    if is_numeric_text_type(native_type):
        return True
    canonical = canonical_builtin_type(native_type)
    if canonical is None:
        return False
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- recursive native grammar.
        nested_descriptor,
    )

    return any(
        contains_numeric_text_type(child) for _name, child in nested_descriptor(canonical)[1]
    )


def validate_numeric_text_type_profile(native_type: str) -> bool:
    """Validate declarations without admitting a raw string profile or using encoders."""
    contains_numeric_text_type(native_type)
    return False


def _declared_type(native_type: str) -> str:
    return canonical_builtin_type(native_type) or native_type


def normalize_authoritative_numeric_text(value: Any, native_type: str | None) -> Any:
    """Normalize numeric text views without changing inference or native-value paths."""
    if native_type is None or not contains_numeric_text_type(native_type):
        return value
    if is_numeric_text_type(native_type):
        return convert_numeric_text(value, native_type)[1]
    from analytics_toolkit.sql.backends.numeric_text_views import (  # noqa: PLC0415 -- text-only container view.
        numeric_text_view,
    )

    return numeric_text_view(value, native_type, convert_numeric_text, _declared_type)


def _authoritative_scalar(value: Any, native_type: str) -> tuple[bool, Any]:
    handled, converted = convert_numeric_text(value, native_type)
    if handled:
        return True, converted
    from analytics_toolkit.sql.backends.gp.integer_values import (  # noqa: PLC0415 -- scalar native preprocessing only.
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

    return numeric_text_view(value, native_type, _authoritative_scalar, _declared_type)


native_text_cast_value = numeric_text_value


__all__ = [
    "GreenplumNumericTextValueError",
    "contains_numeric_text_type",
    "convert_numeric_text",
    "is_numeric_text_type",
    "native_text_cast_value",
    "normalize_authoritative_numeric_text",
    "normalize_authoritative_value",
    "numeric_text_value",
    "validate_numeric_text_type_profile",
]
