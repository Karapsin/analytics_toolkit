"""Finite decimal-text values rounded once to the requested IEEE representation."""

from __future__ import annotations

import math
import struct
from decimal import Decimal

_FLOAT32_MAX_BITS = 0x7F7FFFFF
_FLOAT32_BITS = 32
_INVALID_FLOAT = "Numeric text exceeds the finite floating-point domain."
_ADJUSTED_LIMITS = {_FLOAT32_BITS: (-46, 38), 64: (-324, 308)}


def _float32_bits_value(bits: int) -> float:
    return float(struct.unpack(">f", struct.pack(">I", bits))[0])


def _round_float32(value: Decimal, approximation: float) -> float:
    """Correct a possible binary64-to-binary32 double round using exact midpoints."""
    magnitude = value.copy_abs()
    maximum = _float32_bits_value(_FLOAT32_MAX_BITS)
    previous = _float32_bits_value(_FLOAT32_MAX_BITS - 1)
    # The next mathematical value is 2**128. Its midpoint with the largest
    # finite single rounds to infinity (the even significand), so it is invalid.
    overflow_midpoint = maximum + (maximum - previous) / 2
    if magnitude >= Decimal.from_float(overflow_midpoint):
        raise ValueError(_INVALID_FLOAT)
    absolute = abs(approximation)
    bits = (
        _FLOAT32_MAX_BITS
        if absolute >= maximum
        else struct.unpack(">I", struct.pack(">f", absolute))[0]
    )
    current = _float32_bits_value(bits)
    if bits:
        lower_midpoint = (_float32_bits_value(bits - 1) + current) / 2
        lower = Decimal.from_float(lower_midpoint)
        if magnitude < lower or (magnitude == lower and bits % 2):
            bits -= 1
            current = _float32_bits_value(bits)
    if bits < _FLOAT32_MAX_BITS:
        upper_midpoint = (current + _float32_bits_value(bits + 1)) / 2
        upper = Decimal.from_float(upper_midpoint)
        if magnitude > upper or (magnitude == upper and bits % 2):
            bits += 1
    result = _float32_bits_value(bits)
    return -result if value.is_signed() else result


def decimal_float_value(value: Decimal, bits: int, *, reject_underflow: bool = False) -> float:
    """Round finite text once, retaining signed zero and optional native underflow errors."""
    if bits not in _ADJUSTED_LIMITS or not value.is_finite():
        raise ValueError(_INVALID_FLOAT)
    if value.is_zero():
        return -0.0 if value.is_signed() else 0.0
    minimum, maximum = _ADJUSTED_LIMITS[bits]
    exponent = value.adjusted()
    # These bounds are deliberately coarse. Borderline values still use IEEE
    # rounding below; extreme exponents never create powers or large ratios.
    if exponent > maximum:
        raise ValueError(_INVALID_FLOAT)
    if exponent < minimum:
        if reject_underflow:
            raise ValueError(_INVALID_FLOAT)
        return -0.0 if value.is_signed() else 0.0
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError(_INVALID_FLOAT) from None
    if not math.isfinite(result):
        raise ValueError(_INVALID_FLOAT)
    if bits == _FLOAT32_BITS:
        result = _round_float32(value, result)
    if reject_underflow and result == 0 and not value.is_zero():
        raise ValueError(_INVALID_FLOAT)
    return result


__all__ = ["decimal_float_value"]
