"""Exact decimal scale alignment without context rounding or discarded digits."""

from __future__ import annotations

import re
from decimal import Decimal
from functools import lru_cache
from numbers import Integral
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.core.exact_values import decimal_digits

if TYPE_CHECKING:
    from .value_profiles import ColumnProfile

_MAX_DECLARED_PRECISION = {"gp": 1000, "trino": 38, "ch": 76}
_GP_INTEGRAL_DIGITS = 131072
_GP_FRACTIONAL_DIGITS = 16383
_FIXED_DECIMAL_ARGUMENT_COUNT = 2
_DECIMAL_NAMES = {
    "numeric",
    "decimal",
    "number",
    "decimal32",
    "decimal64",
    "decimal128",
    "decimal256",
}


def _decimal_parameter(value: str) -> int:
    if re.fullmatch(r"[+-]?[0-9]+", value, flags=re.ASCII) is None:
        msg = "Authoritative decimal type has invalid precision or scale."
        raise ValueError(msg)
    try:
        return int(value)
    except ValueError:
        msg = "Authoritative decimal type has invalid precision or scale."
        raise ValueError(msg) from None


def decimal_target_type(target_type: str, backend: str) -> str:
    """Resolve backend-native decimal aliases/defaults for internal validation."""
    if backend not in {"trino", "ch"}:
        return target_type
    from .type_resolution import parse_native_type  # noqa: PLC0415 - native grammar cycle

    native = parse_native_type(target_type)
    if (
        backend == "ch"
        and native.name in {"nullable", "lowcardinality"}
        and len(native.arguments) == 1
    ):
        child = decimal_target_type(native.arguments[0], backend)
        return f"{native.name}({child})" if child != native.arguments[0] else target_type
    aliases = {"decimal", "numeric", "dec", "fixed"} if backend == "ch" else {"decimal"}
    if native.name in aliases:
        if len(native.arguments) < _FIXED_DECIMAL_ARGUMENT_COUNT:
            precision = (
                _decimal_parameter(native.arguments[0])
                if native.arguments
                else (10 if backend == "ch" else 38)
            )
            return f"{'Decimal' if backend == 'ch' else 'DECIMAL'}({precision},0)"
        if backend == "ch" and native.name != "decimal":
            return "Decimal(" + ",".join(native.arguments) + ")"
    return target_type


@lru_cache(maxsize=1024)
def validate_decimal_type(target_type: str, backend: str) -> None:
    """Validate recognized decimal declarations independently of input values."""
    from .nested_types import nested_descriptor  # noqa: PLC0415 - declared container grammar
    from .type_resolution import (  # noqa: PLC0415 - native grammar/profile cycle
        decimal_domain,
        parse_native_type,
    )

    target_type = decimal_target_type(target_type, backend)
    native = parse_native_type(target_type).base
    if native.name not in _DECIMAL_NAMES:
        for _name, child in nested_descriptor(target_type)[1]:
            validate_decimal_type(child, backend)
        return
    allowed_names = {
        "gp": {"numeric", "decimal"},
        "trino": {"decimal"},
        "ch": _DECIMAL_NAMES - {"numeric", "number"},
        "": _DECIMAL_NAMES,
    }
    if native.name not in allowed_names[backend]:
        msg = "Authoritative decimal type is unsupported by this backend."
        raise ValueError(msg)
    if (
        not native.arguments
        and backend in {"gp", ""}
        and native.name
        in {
            "numeric",
            "decimal",
            "number",
        }
    ):
        return
    try:
        shape = decimal_domain(target_type)
    except ValueError:
        msg = "Authoritative decimal type has invalid precision or scale."
        raise ValueError(msg) from None
    if (
        shape is None
        and backend in {"gp", ""}
        and native.name in {"numeric", "decimal", "number"}
        and len(native.arguments) == 1
    ):
        shape = _decimal_parameter(native.arguments[0]), 0
    if shape is None or not (
        1 <= shape[0] <= _MAX_DECLARED_PRECISION.get(backend, 1000) and 0 <= shape[1] <= shape[0]
    ):
        msg = "Authoritative decimal type exceeds supported precision or scale."
        raise ValueError(msg)


def minimal_fractional_scale(value: Decimal) -> int:
    """Count only fractional places ending in a nonzero coefficient digit."""
    decimal_digits(value)  # Reject non-finite values before interpreting the exponent.
    if value.is_zero():
        return 0
    _, digits, exponent = value.as_tuple()
    scale = max(-int(exponent), 0)
    for digit in reversed(digits[max(len(digits) - scale, 0) :]):
        if digit:
            break
        scale -= 1
    return scale


def required_fractional_scale(profile: ColumnProfile) -> int:
    """Use raw scale unless observed values prove a smaller exact requirement."""
    proven = profile.minimal_nonzero_fractional_scale
    return profile.fractional_digits if proven is None else proven


def _decimal_type(profile: ColumnProfile, backend: str, scale: int) -> str | None:
    precision = max(1, profile.integral_digits + scale)
    if backend == "gp" and precision > _MAX_DECLARED_PRECISION[backend]:
        return (
            "NUMERIC"
            if profile.integral_digits <= _GP_INTEGRAL_DIGITS and scale <= _GP_FRACTIONAL_DIGITS
            else None
        )
    if precision > _MAX_DECLARED_PRECISION[backend]:
        return None
    name = {"gp": "NUMERIC", "trino": "DECIMAL", "ch": "Decimal"}[backend]
    return f"{name}({precision}, {scale})"


def resolve_decimal_type(profile: ColumnProfile, backend: str) -> str | None:
    """Prefer the observed scale, then try its proven minimal exact scale."""
    preferred = _decimal_type(profile, backend, profile.fractional_digits)
    return preferred or _decimal_type(profile, backend, required_fractional_scale(profile))


def _align_fractional_zeros(value: Decimal, scale: int) -> Decimal:
    sign, digits, exponent = value.as_tuple()
    excess = max(-int(exponent) - scale, 0)
    if not excess:
        return value
    # The caller proved all discarded positions are zero. Constructing from
    # coefficient/exponent avoids Decimal.quantize/normalize context rounding.
    aligned = digits[:-excess] if excess < len(digits) else (0,)
    return Decimal((sign, aligned, int(exponent) + excess))


def convert_decimal_value(value: Any, target_type: str, backend: str) -> tuple[bool, Any]:
    """Align accepted decimal payloads to an authoritative native destination."""
    from .type_resolution import (  # noqa: PLC0415 - recursive authoritative native grammar.
        decimal_domain,
        parse_native_type,
    )

    target_type = decimal_target_type(target_type, backend)
    validate_decimal_type(target_type, backend)
    shape = decimal_domain(target_type)
    base = parse_native_type(target_type).base
    if shape is None and base.name not in {"numeric", "decimal", "number"}:
        return False, value
    if not isinstance(value, (Decimal, Integral)):
        return False, value
    numeric = value if isinstance(value, Decimal) else Decimal(int(value))
    if shape is None and base.arguments:
        shape = int(base.arguments[0]), 0
    if shape is None:
        integral_limit, scale = _GP_INTEGRAL_DIGITS, _GP_FRACTIONAL_DIGITS
    else:
        precision, scale = shape
        integral_limit = precision - scale
    integral, _ = decimal_digits(numeric)
    if integral > integral_limit or minimal_fractional_scale(numeric) > scale:
        msg = "Selected input exceeds authoritative decimal precision or scale."
        raise ValueError(msg)
    return True, _align_fractional_zeros(numeric, scale)
