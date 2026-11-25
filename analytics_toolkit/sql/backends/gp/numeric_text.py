"""Greenplum numeric text retains native special values and finite constraints."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from numbers import Integral, Real
from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.sql.backends.finite_numeric_text import decimal_float_value
from analytics_toolkit.sql.backends.gp.type_identity import canonical_builtin_type
from analytics_toolkit.sql.backends.native_scalars import NativeDecimalNaN, NativeFloatNaN

if TYPE_CHECKING:
    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile

_ASCII_WHITESPACE = " \t\r\n\v\f"
_FINITE_DECIMAL = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?",
    flags=re.ASCII,
)
_NUMERIC_TYPE = re.compile(r"NUMERIC(?:\([0-9]+, [0-9]+\))?", flags=re.ASCII)
_INVALID_NUMERIC_TEXT = (
    "Greenplum numeric input requires supported ASCII numeric text within the declared "
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


def numeric_text_value(value: str, native_type: str) -> Decimal | float | str:
    """Parse proven native input and defer version-dependent numeric infinity."""
    kind = _numeric_kind(native_type)
    if kind is None or not isinstance(value, str) or not value.isascii():
        raise GreenplumNumericTextValueError(_INVALID_NUMERIC_TEXT)
    token = value.strip(_ASCII_WHITESPACE)
    special = token.lower()
    if special == "nan":
        return NativeDecimalNaN() if kind == "decimal" else NativeFloatNaN()
    if special in {"infinity", "+infinity", "-infinity", "inf", "+inf", "-inf"}:
        # psycopg2's Decimal infinity adapter can emit NUMERIC NaN. Keep the
        # native textual input so the actual server decides version/typmod
        # support in an owned assignment checkpoint.
        return value if kind == "decimal" else float(token)
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


def convert_authoritative_numeric(value: Any, native_type: str) -> tuple[bool, Any]:
    """Retain the established explicit float spelling conversion only for authority."""
    if (
        _numeric_kind(native_type) == "decimal"
        and isinstance(value, Real)
        and not isinstance(value, Integral)
    ):
        numeric = Decimal(str(float(value)))
        if numeric.is_qnan():
            return True, NativeDecimalNaN(numeric)
        if not numeric.is_finite() or canonical_builtin_type(native_type) == "NUMERIC":
            return True, numeric
        from analytics_toolkit.sql.backends import (  # noqa: PLC0415 -- resolver cycle.
            decimal_values,
        )

        return True, decimal_values.convert_decimal_value(numeric, native_type, "gp")[1]
    handled, converted = convert_numeric_text(value, native_type)
    if handled:
        return True, converted
    return False, value


def _authoritative_scalar(value: Any, native_type: str) -> tuple[bool, Any]:
    handled, converted = convert_authoritative_numeric(value, native_type)
    if handled:
        return True, converted
    from analytics_toolkit.sql.backends.gp.integer_values import (  # noqa: PLC0415 -- scalar native preprocessing only.
        normalize_authoritative_integer,
    )
    from analytics_toolkit.sql.backends.gp.network_values import (  # noqa: PLC0415 -- supported driver values.
        normalize_authoritative_network,
    )

    return True, normalize_authoritative_network(
        normalize_authoritative_integer(value, native_type), native_type
    )


def normalize_authoritative_value(value: Any, native_type: str | None) -> Any:
    """Compose native scalar views without eagerly converting mixed containers."""
    if native_type is None:
        return value
    from analytics_toolkit.sql.backends.numeric_text_views import (  # noqa: PLC0415 -- one-pass scalar view.
        numeric_text_view,
    )

    return numeric_text_view(value, native_type, _authoritative_scalar, _declared_type)


native_text_cast_value = numeric_text_value


def _without_native_specials(profile: ColumnProfile) -> ColumnProfile:
    from dataclasses import replace  # noqa: PLC0415 -- bounded profile-only view.

    return replace(
        profile,
        families=profile.families - {"nonfinite", "string"},
        children=[_without_native_specials(child) for child in profile.children],
        array_leaves=_without_native_specials(profile.array_leaves)
        if profile.array_leaves is not None
        else None,
    )


def pending_numeric_assignment(
    profile: ColumnProfile,
    native_type: str,
    *,
    allow_timestamp_precision_loss: bool = True,
    destination_timezone: str | None = None,
) -> bool:
    """Certify actual special-value support without waiving finite constraints."""
    if not contains_numeric_text_type(native_type):
        return False
    scalar = canonical_builtin_type(native_type) or native_type
    array = scalar.endswith("[]")
    while scalar.endswith("[]"):
        scalar = scalar[:-2].rstrip()
    leaf = profile.array_leaves if array and profile.array_leaves is not None else profile
    if not (
        "nonfinite" in leaf.families
        or (_numeric_kind(scalar) == "decimal" and getattr(leaf, "numeric_special_text", False))
    ):
        return False
    from analytics_toolkit.sql.backends.type_resolution import validate_profile  # noqa: PLC0415

    validate_profile(
        _without_native_specials(profile),
        native_type,
        "gp",
        allow_timestamp_precision_loss=allow_timestamp_precision_loss,
        destination_timezone=destination_timezone,
    )
    return True


def numeric_assignment_value(
    value: Any, native_type: str, *, destination_timezone: str | None = None
) -> Any:
    """Keep raw special inputs until the real GP parameter/assignment boundary."""
    del destination_timezone
    from dataclasses import replace  # noqa: PLC0415 -- immutable source array view.

    from analytics_toolkit.sql.backends.native_scalars import ExactArray  # noqa: PLC0415

    value = normalize_authoritative_value(value, native_type)
    scalar = canonical_builtin_type(native_type) or native_type
    if scalar.endswith("[]"):
        while scalar.endswith("[]"):
            scalar = scalar[:-2].rstrip()
        if isinstance(value, ExactArray):
            return replace(
                value, values=tuple(numeric_assignment_value(item, scalar) for item in value.values)
            )
        if isinstance(value, (list, tuple)):
            return [numeric_assignment_value(item, native_type) for item in value]
    return str(value) if isinstance(value, Decimal) and value.is_infinite() else value


__all__ = [
    "GreenplumNumericTextValueError",
    "contains_numeric_text_type",
    "convert_authoritative_numeric",
    "convert_numeric_text",
    "is_numeric_text_type",
    "native_text_cast_value",
    "normalize_authoritative_numeric_text",
    "normalize_authoritative_value",
    "numeric_assignment_value",
    "numeric_text_value",
    "pending_numeric_assignment",
    "validate_numeric_text_type_profile",
]
