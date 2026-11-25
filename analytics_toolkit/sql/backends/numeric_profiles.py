"""Aggregate proofs for exact inferred conversions between numeric families."""

from __future__ import annotations

import math
from dataclasses import replace
from decimal import Decimal
from numbers import Integral, Real
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.core.exact_values import decimal_digits

from .decimal_values import minimal_fractional_scale, required_fractional_scale

if TYPE_CHECKING:
    from .value_profiles import ColumnProfile


def observe_integer_float_domain(profile: ColumnProfile, value: int) -> None:
    try:
        exact = int(float(value)) == value
    except OverflowError:
        exact = False
    profile.integers_fit_float64 &= exact


def observe_float_decimal_domain(profile: ColumnProfile, value: Real) -> None:
    number = float(value)
    profile.float_negative_zero |= number == 0 and math.copysign(1.0, number) < 0
    if not math.isfinite(number):
        profile.floating_values_finite = False
        return
    numeric = Decimal.from_float(number)
    integral, fractional = decimal_digits(numeric)
    profile.float_integral_digits = max(profile.float_integral_digits, integral)
    profile.float_fractional_digits = max(profile.float_fractional_digits, fractional)
    profile.float_minimal_fractional_scale = max(
        profile.float_minimal_fractional_scale, minimal_fractional_scale(numeric)
    )


def observe_decimal_float_domain(profile: ColumnProfile, value: Decimal) -> None:
    try:
        exact = value.is_finite() and Decimal.from_float(float(value)) == value
    except (ValueError, OverflowError):
        exact = False
    profile.decimals_fit_float64 &= exact


def exact_decimal_profile(profile: ColumnProfile) -> ColumnProfile:
    """Use exact binary floating values, never their shortened display strings."""
    return replace(
        profile,
        families=(profile.families - {"float"}) | {"decimal"},
        integral_digits=max(profile.integral_digits, profile.float_integral_digits),
        fractional_digits=max(profile.fractional_digits, profile.float_fractional_digits),
        minimal_nonzero_fractional_scale=max(
            required_fractional_scale(profile), profile.float_minimal_fractional_scale
        ),
    )


def merge_numeric_facts(profile: ColumnProfile, other: ColumnProfile) -> None:
    profile.integers_fit_float64 &= other.integers_fit_float64
    profile.decimals_fit_float64 &= other.decimals_fit_float64
    profile.floating_values_finite &= other.floating_values_finite
    profile.float_negative_zero |= other.float_negative_zero
    for field in (
        "float_integral_digits",
        "float_fractional_digits",
        "float_minimal_fractional_scale",
    ):
        setattr(profile, field, max(getattr(profile, field), getattr(other, field)))


def inferred_numeric_value(value: Any, target_type: str) -> Any:
    """Apply only an inference-certified numeric conversion to the replay view."""
    from .type_resolution import (  # noqa: PLC0415 -- resolved native grammar cycle.
        decimal_domain,
        parse_native_type,
    )

    if (
        isinstance(value, Real)
        and not isinstance(value, Integral)
        and (
            decimal_domain(target_type) is not None
            or parse_native_type(target_type).base.name in {"numeric", "decimal"}
        )
    ):
        return Decimal.from_float(float(value))
    return value
