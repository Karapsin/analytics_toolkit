"""Finite Trino numeric text follows native CAST grammar before profiling."""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, cast

from analytics_toolkit.sql.backends.finite_numeric_text import decimal_float_value
from analytics_toolkit.sql.backends.native_scalars import NativeFloatNaN

_JAVA_TRIM = "".join(chr(index) for index in range(33))
_JAVA_CHAR_MAX = 0xFFFF
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", re.ASCII)
_HEX_FLOAT = re.compile(
    r"([+-]?)0[xX]([0-9a-fA-F]+(?:\.[0-9a-fA-F]*)?|\.[0-9a-fA-F]+)[pP]([+-]?[0-9]+)",
    re.ASCII,
)
_FLOAT_NAMES = {"real": 32, "double": 64, "double precision": 64}
_INVALID = (
    "Trino numeric text requires a finite native numeric value that fits the declared type. "
    "For other conversions, CAST the source expression to the required type in from_sql."
)


class TrinoNumericTextValueError(ValueError):
    """Malformed, nonfinite or out-of-range text is not retryable."""

    analytics_toolkit_sql_retry_safe = False


@lru_cache(maxsize=1024)
def is_numeric_text_type(native_type: str | None) -> bool:
    from analytics_toolkit.sql.backends.decimal_values import (  # noqa: PLC0415 -- registry initialization.
        decimal_target_type,
        validate_decimal_type,
    )
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar cycle.
        parse_native_type,
    )

    raw = parse_native_type(native_type)
    native = parse_native_type(decimal_target_type(native_type or "", "trino"))
    if native.base.name == "decimal":
        validate_decimal_type(native_type or "", "trino")
        if native.name != "decimal" or (not raw.arguments and raw.original.casefold() != "decimal"):
            raise TrinoNumericTextValueError(_INVALID)
        return True
    if native.base.name not in _FLOAT_NAMES:
        return False
    if (
        native.name != native.base.name
        or native.arguments
        or " ".join(native.original.casefold().split()) != native.base.name
    ):
        raise TrinoNumericTextValueError(_INVALID)
    return True


def _decimal_text(value: str) -> Decimal:
    # BigDecimal accepts BMP decimal digits, without trimming or separators.
    normalized = []
    for character in value:
        if character.isdecimal():
            if ord(character) > _JAVA_CHAR_MAX:
                raise TrinoNumericTextValueError(_INVALID)
            normalized.append(str(int(character)))
        else:
            normalized.append(character)
    token = "".join(normalized)
    if _DECIMAL.fullmatch(token) is None:
        raise TrinoNumericTextValueError(_INVALID)
    return Decimal(token)


def _hex_decimal(token: str, match: re.Match[str]) -> Decimal | float:
    approximate = float.fromhex(token)
    sign, significand, exponent = match.groups()
    digits = significand.replace(".", "")
    # Underflow is a valid JDK floating cast; avoid constructing an enormous
    # coefficient for an exponent whose finite result is already signed zero.
    if approximate == 0.0:
        return approximate
    exponent_digits = exponent.lstrip("+-").lstrip("0") or "0"
    power = int(exponent_digits) * (-1 if exponent.startswith("-") else 1)
    power -= 4 * (len(significand.partition(".")[2]))
    if abs(power) > 4 * len(digits) + 1075:
        raise TrinoNumericTextValueError(_INVALID)
    coefficient = int(digits, 16)
    if power >= 0:
        exact = Decimal(coefficient << power)
    else:
        exact_coefficient = Decimal(coefficient * 5 ** (-power))
        exact = Decimal((0, exact_coefficient.as_tuple().digits, power))
    return exact.copy_negate() if sign == "-" else exact


def _floating_text(value: str, bits: int) -> float:
    # NumberParser deliberately falls back to Float/Double.parseFloat/Double.
    token = value.strip(_JAVA_TRIM)
    if not token.isascii():
        raise TrinoNumericTextValueError(_INVALID)
    if token in {"Infinity", "+Infinity", "-Infinity", "NaN", "+NaN", "-NaN"}:
        number = float(token)
        return NativeFloatNaN(number) if math.isnan(number) else number
    if token.endswith(("f", "F", "d", "D")):
        token = token[:-1]
    if _DECIMAL.fullmatch(token) is not None:
        exact: Decimal | float = Decimal(token)
    else:
        match = _HEX_FLOAT.fullmatch(token)
        if match is None:
            raise TrinoNumericTextValueError(_INVALID)
        exact = _hex_decimal(token, match)
    return exact if isinstance(exact, float) else decimal_float_value(exact, bits)


def numeric_text_value(value: Any, native_type: str) -> Decimal | float:
    """Validate finite native text casts without introducing decimal rounding."""
    from analytics_toolkit.sql.backends.decimal_values import (  # noqa: PLC0415 -- decimal/profile initialization.
        convert_decimal_value,
        decimal_target_type,
    )
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar/profile cycle.
        ColumnProfile,
        parse_native_type,
        validate_profile,
    )

    if not isinstance(value, str) or not is_numeric_text_type(native_type):
        raise TrinoNumericTextValueError(_INVALID)
    result: Decimal | float
    try:
        canonical = decimal_target_type(native_type, "trino")
        native = parse_native_type(canonical).base
        if native.name == "decimal":
            parsed = _decimal_text(value)
            _handled, converted = convert_decimal_value(parsed, canonical, "trino")
            result = cast("Decimal", converted)
        else:
            result = _floating_text(value, _FLOAT_NAMES[native.name])
            profile = ColumnProfile(native_type=canonical, source_backend="trino")
            profile.add(result)
            validate_profile(profile, canonical, "trino")
    except (ValueError, TypeError, OverflowError, InvalidOperation):
        raise TrinoNumericTextValueError(_INVALID) from None
    return result


def convert_numeric_text(value: Any, native_type: str) -> tuple[bool, Any]:
    """Leave every existing nontext scalar conversion path unchanged."""
    if not isinstance(value, str) or not is_numeric_text_type(native_type):
        return False, value
    return True, numeric_text_value(value, native_type)


@lru_cache(maxsize=1024)
def contains_numeric_text_type(native_type: str) -> bool:
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415 -- recursive native grammar.
        nested_descriptor,
    )

    if is_numeric_text_type(native_type):
        return True
    children = [
        contains_numeric_text_type(child) for _name, child in nested_descriptor(native_type)[1]
    ]
    return any(children)


def validate_numeric_text_type_profile(native_type: str) -> bool:
    """Check declarations even for empty inputs, admitting no value family."""
    contains_numeric_text_type(native_type)
    return False


def _declared_type(native_type: str) -> str:
    return native_type


def normalize_authoritative_numeric_text(value: Any, native_type: str | None) -> Any:
    """Build a numeric validation view while leaving original cells intact."""
    if native_type is None or not contains_numeric_text_type(native_type):
        return value
    if is_numeric_text_type(native_type):
        return numeric_text_value(value, native_type) if isinstance(value, str) else value
    from analytics_toolkit.sql.backends.numeric_text_views import (  # noqa: PLC0415 -- text-only container view.
        numeric_text_view,
    )

    return numeric_text_view(value, native_type, convert_numeric_text, _declared_type)


def _authoritative_scalar(value: Any, native_type: str) -> tuple[bool, Any]:
    handled, converted = convert_numeric_text(value, native_type)
    if handled:
        return True, converted
    from analytics_toolkit.sql.backends.trino.integer_values import (  # noqa: PLC0415 -- scalar native preprocessing only.
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


# Direct Trino scalar text and an explicit VARCHAR-stage SQL CAST use the same
# native parser.  Unlike BIGINT's Python preprocessing, there is no second grammar.
native_text_cast_value = numeric_text_value


__all__ = [
    "TrinoNumericTextValueError",
    "contains_numeric_text_type",
    "convert_numeric_text",
    "is_numeric_text_type",
    "native_text_cast_value",
    "normalize_authoritative_numeric_text",
    "normalize_authoritative_value",
    "numeric_text_value",
    "validate_numeric_text_type_profile",
]
