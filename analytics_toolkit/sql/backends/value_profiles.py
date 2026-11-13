"""Complete exact-value profiles shared by backend resolution policies."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from math import gcd
from numbers import Integral, Real
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd

from analytics_toolkit.sql.core.exact_values import (
    ExactTemporal,
    OrderedMap,
    OrderedMapPairs,
    decimal_digits,
    integer_text,
    is_null,
    typed_text,
    unwrap_enum,
)

from .decimal_values import minimal_fractional_scale, required_fractional_scale
from .nested_values import array_item

_MAP_ARGUMENT_COUNT = 2
_GP_MAX_ARRAY_DIMENSIONS = 6


@dataclass
class ColumnProfile:
    count: int = 0
    null_count: int = 0
    families: set[str] = field(default_factory=set)
    minimum_integer: int | None = None
    maximum_integer: int | None = None
    integral_digits: int = 0
    fractional_digits: int = 0
    temporal_scale: int = 0
    aware: bool = False
    naive: bool = False
    minimum_date: dt.date | None = None
    maximum_date: dt.date | None = None
    minimum_timestamp_ns: int | None = None
    maximum_timestamp_ns: int | None = None
    children: list[ColumnProfile] = field(default_factory=list)
    structurally_compatible: bool = True
    tuple_width: int | None = None
    maximum_numeric_magnitude: Decimal = Decimal(0)
    all_uuid_strings: bool = True
    maximum_text_length: int = 0
    array_leaves: ColumnProfile | None = None
    gp_array_compatible: bool = True
    map_duplicate_keys: bool = False
    map_null_keys: bool = False
    map_comparable_keys: bool = True
    native_type: str | None = None
    source_backend: str | None = None
    minimum_date_only: dt.date | None = None
    maximum_date_only: dt.date | None = None
    destination_timezone: str | None = None
    temporal_type: str | None = None
    temporal_value_count: int = 0
    context_value_count: int = 0
    context_valid: bool = True
    wall_time_error: str | None = None
    wall_time_error_zone: str | None = None
    minimum_context_timestamp_ns: int | None = None
    maximum_context_timestamp_ns: int | None = None
    minimum_naive_timestamp_ns: int | None = None
    maximum_naive_timestamp_ns: int | None = None
    minimum_instant_timestamp_ns: int | None = None
    maximum_instant_timestamp_ns: int | None = None
    maximum_byte_length: int = 0
    text_has_nul: bool = False
    utf8_valid: bool = True
    native_constraints_valid: bool = True
    text_encoding_valid: bool = True
    interval_days_nonzero: bool = False
    interval_time_gcd: int = 0
    time_zone_unresolved: bool = False
    minimal_nonzero_fractional_scale: int | None = None
    trino_time_constraints_valid: bool = True

    def add(self, value: Any) -> None:  # noqa: C901, PLR0912, PLR0915 - family profile dispatch
        value = array_item(value)
        self.count += 1
        if is_null(value):
            self.null_count += 1
            return
        from .nested_types import nested_descriptor  # noqa: PLC0415 - bounded source schema hints

        kind, child_types = nested_descriptor(self.native_type or "")
        if self.native_type:
            from .type_resolution import (  # noqa: PLC0415 - scalar grammar cycle
                parse_native_type,
            )

            if parse_native_type(self.native_type).base.name in {"enum8", "enum16", "fixedstring"}:
                from .ch.value_validation import (  # noqa: PLC0415 - scalar grammar cycle
                    validate_constrained_value,
                )

                try:
                    validate_constrained_value(value, self.native_type)
                except ValueError:
                    self.native_constraints_valid = False
        family = _family(value)
        if family == "map" and kind in {"tuple", "row"}:
            names = [name for name, _ in child_types]
            self.structurally_compatible &= isinstance(value, dict) and set(value) == set(names)
            value = tuple(value.get(name) for name in names)
            family = "tuple"
        self.families.add(family)
        try:
            text_length = len(typed_text(value) or "")
        except (TypeError, ValueError):
            # A native value need not implement the unrelated fallback codec.
            # Defer that incompatibility until a text destination is selected.
            self.text_encoding_valid = False
        else:
            self.maximum_text_length = max(self.maximum_text_length, text_length)
        if family == "integer":
            integer = int(value)
            self.minimum_integer = (
                integer if self.minimum_integer is None else min(self.minimum_integer, integer)
            )
            self.maximum_integer = (
                integer if self.maximum_integer is None else max(self.maximum_integer, integer)
            )
            self.integral_digits = max(
                self.integral_digits, len(integer_text(abs(integer))) if integer else 0
            )
            self.maximum_numeric_magnitude = max(
                self.maximum_numeric_magnitude, Decimal(abs(integer))
            )
        elif family == "decimal":
            try:
                integral, scale = decimal_digits(value)
            except ValueError:
                self.families.add("nonfinite")
            else:
                self.integral_digits = max(self.integral_digits, integral)
                self.minimal_nonzero_fractional_scale = max(
                    required_fractional_scale(self), minimal_fractional_scale(value)
                )
                self.fractional_digits = max(self.fractional_digits, scale)
                self.maximum_numeric_magnitude = max(
                    self.maximum_numeric_magnitude, value.copy_abs()
                )
        elif family == "float":
            magnitude = Decimal.from_float(abs(float(value)))
            if magnitude.is_finite():
                self.maximum_numeric_magnitude = max(self.maximum_numeric_magnitude, magnitude)
        elif family == "string":
            self.text_has_nul |= "\x00" in value
            try:
                self.maximum_byte_length = max(self.maximum_byte_length, len(value.encode("utf-8")))
            except UnicodeEncodeError:
                self.utf8_valid = False
            try:
                UUID(value)
            except (ValueError, AttributeError):
                self.all_uuid_strings = False
        elif family == "binary":
            self.maximum_byte_length = max(self.maximum_byte_length, len(value))
        elif family == "timestamp":
            self._observe_temporal(value)
            exact = isinstance(value, ExactTemporal)
            stamp = value.calendar_datetime() if exact else value
            self.aware |= stamp.tzinfo is not None
            self.naive |= stamp.tzinfo is None
            fraction = (
                value.ticks % 10**value.scale
                if exact
                else stamp.microsecond * 1000 + getattr(stamp, "nanosecond", 0)
            )
            scale = value.scale if exact else 9
            while scale and fraction % 10 == 0:
                fraction //= 10
                scale -= 1
            self.temporal_scale = max(self.temporal_scale, scale)
            if exact:
                ticks = value.ticks * 10 ** (9 - value.scale)
                stamp = value.calendar_datetime(utc=True)
            else:
                if stamp.tzinfo is not None:
                    stamp = stamp.astimezone(dt.timezone.utc)
                days = (stamp.date() - dt.date(1970, 1, 1)).days
                ticks = (
                    (days * 86400 + stamp.hour * 3600 + stamp.minute * 60 + stamp.second) * 10**9
                    + stamp.microsecond * 1000
                    + getattr(stamp, "nanosecond", 0)
                )
            self.minimum_timestamp_ns = (
                ticks
                if self.minimum_timestamp_ns is None
                else min(self.minimum_timestamp_ns, ticks)
            )
            self.maximum_timestamp_ns = (
                ticks
                if self.maximum_timestamp_ns is None
                else max(self.maximum_timestamp_ns, ticks)
            )
            self._add_date(stamp.date())
        elif family in {"time", "interval"}:
            from .gp.temporal_values import (  # noqa: PLC0415 -- backend native clock facts.
                observe_temporal_value,
            )

            observe_temporal_value(self, value)
            if family == "time":
                from .trino.time_values import (  # noqa: PLC0415 -- backend clock facts.
                    observe_time_value,
                )

                observe_time_value(self, value)
        elif family == "date":
            self._observe_temporal(value)
            self.minimum_date_only = (
                value if self.minimum_date_only is None else min(self.minimum_date_only, value)
            )
            self.maximum_date_only = (
                value if self.maximum_date_only is None else max(self.maximum_date_only, value)
            )
            self._add_date(value)
        elif family in {"array", "tuple"}:
            values = list(value)
            if family == "array":
                self.gp_array_compatible &= _array_shape(values) is not None
                if self.array_leaves is None:
                    self.array_leaves = ColumnProfile(
                        source_backend=self.source_backend,
                        destination_timezone=self.destination_timezone,
                        temporal_type=self._destination_leaf(),
                    )
                for leaf in _array_leaves(values):
                    self.array_leaves.add(leaf)
                if not self.children:
                    self.children.append(
                        ColumnProfile(
                            native_type=child_types[0][1] if kind == "array" else None,
                            source_backend=self.source_backend,
                            destination_timezone=self.destination_timezone,
                            temporal_type=self._destination_child(0),
                        )
                    )
                for item in values:
                    self.children[0].add(item)
            else:
                if self.tuple_width is None:
                    self.tuple_width = len(values)
                    self.children = [
                        ColumnProfile(
                            native_type=child_types[index][1] if index < len(child_types) else None,
                            source_backend=self.source_backend,
                            destination_timezone=self.destination_timezone,
                            temporal_type=self._destination_child(index),
                        )
                        for index, _ in enumerate(values)
                    ]
                if len(values) != len(self.children):
                    self.structurally_compatible = False
                else:
                    for child, item in zip(self.children, values):
                        child.add(item)
        elif family == "map":
            pairs = value.items() if isinstance(value, dict) else value.pairs
            seen: set[Any] = set()
            if not self.children:
                self.children = [
                    ColumnProfile(
                        native_type=child_types[index][1] if kind == "map" else None,
                        source_backend=self.source_backend,
                        destination_timezone=self.destination_timezone,
                        temporal_type=self._destination_child(index),
                    )
                    for index in range(_MAP_ARGUMENT_COUNT)
                ]
            if len(self.children) != _MAP_ARGUMENT_COUNT:
                self.structurally_compatible = False
            else:
                for original_key, item in pairs:
                    key = unwrap_enum(original_key)
                    self.map_null_keys |= is_null(key)
                    try:
                        normalized_key = key
                        if kind == "map":
                            from .type_resolution import (  # noqa: PLC0415 - backend recursive type policies
                                ResolvedColumn,
                            )

                            normalized_key = ResolvedColumn(
                                child_types[0][1],
                                target_backend=self.source_backend or "",
                                destination_timezone=self.destination_timezone,
                            ).convert(key)
                        self.map_duplicate_keys |= normalized_key in seen
                        seen.add(normalized_key)
                    except (TypeError, ValueError, ArithmeticError):
                        self.map_comparable_keys = False
                    self.children[0].add(key)
                    self.children[1].add(item)

    def _destination_child(self, index: int) -> str | None:
        from .nested_types import nested_descriptor  # noqa: PLC0415 - destination schema context

        fields = nested_descriptor(self.temporal_type or "")[1]
        return fields[index][1] if index < len(fields) else None

    def _destination_leaf(self) -> str | None:
        from .nested_types import nested_descriptor  # noqa: PLC0415 - destination schema context

        native = self.temporal_type or ""
        while nested_descriptor(native)[0] in {"array", "gp_array"}:
            native = nested_descriptor(native)[1][0][1]
        return native or None

    def _observe_temporal(self, value: Any) -> None:
        from .temporal_context import observe_context  # noqa: PLC0415 - exact destination context

        observe_context(self, value)

    def _add_date(self, value: dt.date) -> None:
        self.minimum_date = value if self.minimum_date is None else min(self.minimum_date, value)
        self.maximum_date = value if self.maximum_date is None else max(self.maximum_date, value)

    observe = add

    def merge(self, other: ColumnProfile) -> ColumnProfile:  # noqa: C901 - complete aggregate merge
        from .temporal_context import effective_timezone  # noqa: PLC0415 - frozen context identity

        self.context_valid &= other.context_valid and effective_timezone(
            self.temporal_type, self.destination_timezone
        ) == effective_timezone(other.temporal_type, other.destination_timezone)
        self.wall_time_error_zone = (
            self.wall_time_error_zone
            if self.wall_time_error is not None
            else other.wall_time_error_zone
        )
        self.wall_time_error = self.wall_time_error or other.wall_time_error
        self.temporal_value_count += other.temporal_value_count
        self.context_value_count += other.context_value_count
        self.count += other.count
        self.null_count += other.null_count
        self.families |= other.families
        for attribute, reducer in (
            ("minimum_integer", min),
            ("maximum_integer", max),
            ("minimum_date_only", min),
            ("maximum_date_only", max),
            ("minimum_date", min),
            ("maximum_date", max),
            ("minimum_context_timestamp_ns", min),
            ("maximum_context_timestamp_ns", max),
            ("minimum_naive_timestamp_ns", min),
            ("maximum_naive_timestamp_ns", max),
            ("minimum_instant_timestamp_ns", min),
            ("maximum_instant_timestamp_ns", max),
            ("minimum_timestamp_ns", min),
            ("maximum_timestamp_ns", max),
        ):
            values = [
                value
                for value in (getattr(self, attribute), getattr(other, attribute))
                if value is not None
            ]
            if values:
                setattr(self, attribute, reducer(values))
        self.integral_digits = max(self.integral_digits, other.integral_digits)
        self.minimal_nonzero_fractional_scale = max(
            required_fractional_scale(self), required_fractional_scale(other)
        )
        self.fractional_digits = max(self.fractional_digits, other.fractional_digits)
        self.temporal_scale = max(self.temporal_scale, other.temporal_scale)
        self.aware |= other.aware
        self.naive |= other.naive
        self.maximum_numeric_magnitude = max(
            self.maximum_numeric_magnitude, other.maximum_numeric_magnitude
        )
        self.all_uuid_strings &= other.all_uuid_strings
        self.maximum_text_length = max(self.maximum_text_length, other.maximum_text_length)
        self.text_encoding_valid &= other.text_encoding_valid
        self.maximum_byte_length = max(self.maximum_byte_length, other.maximum_byte_length)
        self.text_has_nul |= other.text_has_nul
        self.utf8_valid &= other.utf8_valid
        self.native_constraints_valid &= other.native_constraints_valid
        self._merge_clock_facts(other)
        self.gp_array_compatible &= other.gp_array_compatible
        self.map_duplicate_keys |= other.map_duplicate_keys
        self.map_null_keys |= other.map_null_keys
        self.map_comparable_keys &= other.map_comparable_keys
        if other.array_leaves is not None:
            if self.array_leaves is None:
                self.array_leaves = other.array_leaves
            else:
                self.array_leaves.merge(other.array_leaves)
        if self.children and other.children and len(self.children) != len(other.children):
            self.structurally_compatible = False
        elif other.children:
            if not self.children:
                self.children = other.children
            else:
                for child, incoming in zip(self.children, other.children):
                    child.merge(incoming)
        self.structurally_compatible &= other.structurally_compatible
        if self.tuple_width is not None and other.tuple_width is not None:
            self.structurally_compatible &= self.tuple_width == other.tuple_width
        elif other.tuple_width is not None:
            self.tuple_width = other.tuple_width
        return self

    def _merge_clock_facts(self, other: ColumnProfile) -> None:
        self.interval_days_nonzero |= other.interval_days_nonzero
        self.interval_time_gcd = gcd(self.interval_time_gcd, other.interval_time_gcd)
        self.time_zone_unresolved |= other.time_zone_unresolved
        self.trino_time_constraints_valid &= other.trino_time_constraints_valid


def _family(value: Any) -> str:  # noqa: C901, PLR0911 - ordered scalar-family dispatch
    from .gp.temporal_values import temporal_family  # noqa: PLC0415 -- native value markers.

    temporal = temporal_family(value)
    if temporal is not None:
        return temporal
    if isinstance(value, (bool, np.bool_)):
        return "boolean"
    if isinstance(value, Integral):
        return "integer"
    if isinstance(value, Decimal):
        return "decimal"
    if isinstance(value, Real):
        return "float"
    if isinstance(value, (dt.datetime, pd.Timestamp, ExactTemporal)):
        return "timestamp"
    if isinstance(value, dt.date):
        return "date"
    if isinstance(value, UUID):
        return "uuid"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "binary"
    if isinstance(value, (dict, OrderedMap, OrderedMapPairs)):
        return "map"
    if isinstance(value, tuple):
        return "tuple"
    if isinstance(value, list):
        return "array"
    return "string" if isinstance(value, str) else "unsupported"


def _array_shape(value: list[Any]) -> tuple[int, ...] | None:
    """GP arrays require rectangular, nonempty inner dimensions (maximum six)."""
    value = [array_item(item) for item in value]
    nested = [isinstance(item, list) for item in value]
    if not any(nested):
        return (len(value),)
    if not all(nested):
        return None
    shapes = [_array_shape(item) for item in value]
    first = shapes[0]
    if first is None or 0 in first or any(shape != first for shape in shapes):
        return None
    shape = (len(value), *first)
    return shape if len(shape) <= _GP_MAX_ARRAY_DIMENSIONS else None


def _array_leaves(value: list[Any]) -> Any:
    for original in value:
        item = array_item(original)
        if isinstance(item, list):
            yield from _array_leaves(item)
        else:
            yield item
