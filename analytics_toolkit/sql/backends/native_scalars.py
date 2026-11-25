"""Immutable decoded native values independent of adapter initialization."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from typing_extensions import Self


class JsonDocument(str):
    """Encoded JSON text, including JSON null, distinct from SQL NULL."""

    __slots__ = ()


class NativeFloatNaN(float):
    """A decoded native NaN, distinct from a dataframe's missing-value marker."""

    __slots__ = ()

    def __new__(cls, value: float = float("nan")) -> Self:
        if not isinstance(value, float) or not math.isnan(value):
            message = "A native floating NaN carrier requires a decoded NaN."
            raise ValueError(message)
        return float.__new__(cls, value)


class NativeDecimalNaN(Decimal):
    """A decoded native numeric NaN, distinct from SQL NULL."""

    __slots__ = ()

    def __new__(cls, value: Decimal = Decimal("NaN")) -> Self:
        if not isinstance(value, Decimal) or not value.is_qnan():
            message = "A native numeric NaN carrier requires a decoded quiet NaN."
            raise ValueError(message)
        return Decimal.__new__(cls, value)


@dataclass(frozen=True)
class ExactInterval:
    """Independent SQL calendar months, calendar days and microseconds."""

    months: int
    days: int
    microseconds: int

    def __post_init__(self) -> None:
        if any(type(value) is not int for value in (self.months, self.days, self.microseconds)):
            message = "Exact interval components must be integers."
            raise ValueError(message)


@dataclass(frozen=True)
class ExactArray:
    """Flat native array cells with their actual dimension lengths and bounds."""

    values: tuple[Any, ...]
    dimensions: tuple[int, ...]
    lower_bounds: tuple[int, ...]

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, tuple) for value in (self.values, self.dimensions, self.lower_bounds)
        ):
            message = "Exact array cells and dimensions must be immutable tuples."
            raise ValueError(message)
        size = 1 if self.dimensions else 0
        if len(self.dimensions) != len(self.lower_bounds):
            message = "Exact array dimensions and lower bounds must have equal rank."
            raise ValueError(message)
        for length, lower in zip(self.dimensions, self.lower_bounds):
            if type(length) is not int or length < 0 or type(lower) is not int:
                message = "Exact array dimensions and bounds must be integers of valid size."
                raise ValueError(message)
            size *= length
        if size != len(self.values):
            message = "Exact array cell count differs from its dimensions."
            raise ValueError(message)

    def nested_values(self) -> list[Any]:
        """Expose a shaped value view while retaining bounds on this carrier."""
        values = iter(self.values)

        def reshape(dimensions: tuple[int, ...]) -> list[Any]:
            if len(dimensions) == 1:
                return [next(values) for _ in range(dimensions[0])]
            return [reshape(dimensions[1:]) for _ in range(dimensions[0])]

        return reshape(self.dimensions) if self.dimensions else []
