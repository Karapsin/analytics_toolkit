from __future__ import annotations

import math
from numbers import Real
from typing import Any


def validate_positive_int(value: Any, option_name: str) -> int:
    if value.__class__ is not int or value < 1:
        raise ValueError(f"{option_name} must be a positive integer.")
    return int(value)


def validate_optional_positive_int(
    value: Any,
    option_name: str,
) -> int | None:
    if value is None:
        return None
    return validate_positive_int(value, option_name)


def validate_non_negative_number(value: Any, option_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{option_name} must be a finite non-negative number.")
    resolved = float(value)
    if not math.isfinite(resolved) or resolved < 0:
        raise ValueError(f"{option_name} must be a finite non-negative number.")
    return resolved


def validate_positive_number(value: Any, option_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{option_name} must be a finite positive number.")
    resolved = float(value)
    if not math.isfinite(resolved) or resolved <= 0:
        raise ValueError(f"{option_name} must be a finite positive number.")
    return resolved


def validate_optional_positive_number(
    value: Any,
    option_name: str,
) -> float | None:
    if value is None:
        return None
    return validate_positive_number(value, option_name)
