"""Reject unsupported exact domains before incidental Python coercions."""

from __future__ import annotations

from numbers import Integral, Real
from typing import Any

import numpy as np


def array_values(value: Any) -> Any:
    """Normalize supported ndarray containers without erasing temporal units."""
    if not isinstance(value, np.ndarray):
        return value
    if value.dtype.kind in {"m", "M"}:
        msg = "Unsupported exact NumPy temporal array; supply explicitly converted temporal cells."
        raise TypeError(msg)
    return value.tolist()


def validate_real_value(value: Any) -> None:
    """The supported real domain is builtin/NumPy binary64 or narrower floats."""
    if not isinstance(value, Real) or isinstance(value, (Integral, bool, np.bool_)):
        return
    if isinstance(value, float):
        return
    if isinstance(value, np.floating) and np.finfo(value.dtype).nmant <= np.finfo(np.float64).nmant:
        return
    if isinstance(value, np.floating) and np.isnan(value):
        # Ordinary dataframe NaN denotes a missing cell even in a wider dtype.
        return
    msg = "Unsupported exact real value; supply an explicit supported integer, Decimal or float."
    raise TypeError(msg)
