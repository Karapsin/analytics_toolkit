"""Lossless internal values shared by inference, staging and replay checkpoints."""

from __future__ import annotations

import base64
import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from numbers import Integral, Real
from typing import TYPE_CHECKING, Any, Tuple
from uuid import UUID

import numpy as np
import pandas as pd

from .temporal_zones import canonical_named_zone

if TYPE_CHECKING:
    from collections.abc import Sequence

ENCODING_VERSION = 1
MAX_TEMPORAL_SCALE = 9
_PYTHON_TEMPORAL_SCALE = 6
_INTEGER_CHUNK_DIGITS = 1000
_INTEGER_CHUNK_BASE = 10**_INTEGER_CHUNK_DIGITS
_LEGACY_TICKS_FIELDS = 3


def integer_text(value: int) -> str:
    """Base ten without changing Python's process-wide integer digit limit."""
    sign = "-" if value < 0 else ""
    remaining = abs(value)
    chunks: list[str] = []
    while remaining >= _INTEGER_CHUNK_BASE:
        remaining, remainder = divmod(remaining, _INTEGER_CHUNK_BASE)
        chunks.append(f"{remainder:0{_INTEGER_CHUNK_DIGITS}d}")
    chunks.append(str(remaining))
    return sign + "".join(reversed(chunks))


def _parse_integer_text(value: str) -> int:
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    result = 0
    for start in range(0, len(digits), _INTEGER_CHUNK_DIGITS):
        chunk = digits[start : start + _INTEGER_CHUNK_DIGITS]
        result = result * 10 ** len(chunk) + int(chunk)
    return -result if negative else result


@dataclass(frozen=True)
class OrderedMap:
    """A native Map that cannot be represented by a dict without losing keys."""

    pairs: tuple[tuple[Any, Any], ...]

    def items(self) -> Any:
        return iter(self.pairs)

    def keys(self) -> Any:
        return (key for key, _ in self.pairs)

    def values(self) -> Any:
        return (value for _, value in self.pairs)


class OrderedMapPairs(Tuple[Tuple[Any, Any], ...]):
    """Driver-compatible ordered map items, including repeated keys."""

    __slots__ = ()

    @property
    def pairs(self) -> tuple[tuple[Any, Any], ...]:
        return tuple(self)

    def items(self) -> Any:
        return iter(self)

    def keys(self) -> Any:
        return tuple(key for key, _ in self)

    def values(self) -> Any:
        return tuple(value for _, value in self)


@dataclass(frozen=True)
class ExactTemporal:
    ticks: int
    scale: int
    timezone: str | None = None
    offset_naive: bool = False

    def timestamp(self) -> Any:
        if not 0 <= self.scale <= MAX_TEMPORAL_SCALE:
            msg = "Timestamp scale must be between zero and nine."
            raise ValueError(msg)
        ticks, scale = self.ticks, self.scale
        while scale and ticks % 10 == 0:
            ticks //= 10
            scale -= 1
        unit_scale = ((scale + 2) // 3) * 3
        unit = {0: "s", 3: "ms", 6: "us", 9: "ns"}[unit_scale]
        try:
            value = pd.Timestamp(ticks * 10 ** (unit_scale - scale), unit=unit)
        except (ValueError, OverflowError):
            return self.calendar_datetime() if scale <= _PYTHON_TEMPORAL_SCALE else self
        if self.timezone:
            value = value.tz_localize("UTC").tz_convert(canonical_named_zone(self.timezone))
            return value.tz_localize(None) if self.offset_naive else value
        return value

    def calendar_datetime(self, *, utc: bool = False) -> dt.datetime:
        """Calendar fields for formatting/profiling; fractional sub-microticks stay raw."""
        seconds, fraction = divmod(self.ticks, 10**self.scale)
        micros = (
            fraction * 10 ** (6 - self.scale)
            if self.scale <= _PYTHON_TEMPORAL_SCALE
            else fraction // 10 ** (self.scale - 6)
        )
        value = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(
            seconds=seconds, microseconds=micros
        )
        if self.timezone is None:
            return value.replace(tzinfo=None)
        try:
            zone = (
                dt.timezone.utc
                if utc and not self.offset_naive
                else canonical_named_zone(self.timezone)
            )
        except ValueError as exc:
            msg = "Unknown exact timestamp timezone."
            raise ValueError(msg) from exc
        converted = value.astimezone(zone)
        return converted.replace(tzinfo=None) if self.offset_naive else converted

    def isoformat(self, *, utc: bool = False) -> str:
        value = self.calendar_datetime(utc=utc)
        fraction = self.ticks % 10**self.scale
        base = value.date().isoformat() + value.strftime("T%H:%M:%S")
        suffix = value.strftime("%z")
        if suffix:
            suffix = suffix[:3] + ":" + suffix[3:]
        return base + (f".{fraction:0{self.scale}d}" if self.scale else "") + suffix


def unwrap_enum(value: Any) -> Any:
    """Use an Enum's logical value before selecting SQL or transport behavior."""
    while isinstance(value, Enum):
        value = value.value
    return value


def is_null(value: Any) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    if isinstance(value, (list, tuple, dict, OrderedMap, bytes, bytearray, memoryview)):
        return False
    try:
        result = pd.isna(value)
        return bool(result) if not hasattr(result, "__len__") else False
    except (TypeError, ValueError):
        return False


def decimal_digits(value: Decimal) -> tuple[int, int]:
    """Required integral digits and fractional scale, without decimal context rounding."""
    if not value.is_finite():
        msg = "Non-finite Decimal has no fixed-point SQL representation."
        raise ValueError(msg)
    _, digits, exponent = value.as_tuple()
    exponent = int(exponent)
    return (0 if value.is_zero() else max(len(digits) + exponent, 0)), max(-exponent, 0)


def decimal_shape(values: Sequence[Decimal]) -> tuple[int, int]:
    shapes = [decimal_digits(value) for value in values]
    integral = max((shape[0] for shape in shapes), default=1)
    scale = max((shape[1] for shape in shapes), default=0)
    return max(1, integral + scale), scale


def dataframe_from_columns(
    columns: Sequence[Sequence[Any]],
    names: Sequence[str],
    types: Sequence[str] | None = None,
    *,
    index: Any = None,
) -> pd.DataFrame:
    """Construct columns positionally, never coercing exact scalars through float."""
    from analytics_toolkit.sql.dml.io.dataframes import (  # noqa: PLC0415 - avoid backend registry import cycles
        dataframe_from_columns as construct,
    )

    del types
    frame = construct(
        names,
        columns,
        row_count=len(columns[0]) if columns else (0 if index is None else len(index)),
    )
    if index is not None:
        frame.index = index
    return frame


def dataframe_from_rows(names: Sequence[str], rows: Sequence[Sequence[Any]]) -> pd.DataFrame:
    from analytics_toolkit.sql.dml.io.dataframes import (  # noqa: PLC0415 - avoid backend registry import cycles
        column_result_from_rows,
        dataframe_from_column_result,
    )

    return dataframe_from_column_result(column_result_from_rows(names, rows))


def truncate_temporal(value: Any, scale: int) -> Any:
    """Apply an explicitly resolved timestamp precision without float arithmetic."""
    if isinstance(value, ExactTemporal):
        if scale >= value.scale:
            return value
        ticks = value.ticks // 10 ** (value.scale - scale)
        return ExactTemporal(ticks, scale, value.timezone, value.offset_naive)
    if isinstance(value, pd.Timestamp) and scale <= _PYTHON_TEMPORAL_SCALE:
        value = value.to_pydatetime(warn=False)
    if isinstance(value, dt.datetime) and scale < _PYTHON_TEMPORAL_SCALE:
        factor = 10 ** (_PYTHON_TEMPORAL_SCALE - scale)
        return value.replace(microsecond=(value.microsecond // factor) * factor)
    if isinstance(value, pd.Timestamp) and scale < MAX_TEMPORAL_SCALE:
        factor = 10 ** (MAX_TEMPORAL_SCALE - scale)
        return value.replace(nanosecond=(value.nanosecond // factor) * factor)
    return value


def encode_value(value: Any) -> Any:  # noqa: C901, PLR0911, PLR0912 - tagged scalar codec
    """Tagged JSON tree; SQL NULL and nested containers retain their identity."""
    value = unwrap_enum(value)
    if isinstance(value, np.ndarray):
        return encode_value(value.tolist())
    if isinstance(value, Real) and not isinstance(value, (Integral, bool, np.bool_)):
        return ["float", float(value).hex()]
    if isinstance(value, Decimal):
        return ["decimal", str(value)]
    if is_null(value):
        return ["null"]
    if isinstance(value, (bool, np.bool_)):
        return ["bool", bool(value)]
    if isinstance(value, Integral):
        return ["int", integer_text(int(value))]
    if isinstance(value, ExactTemporal):
        return ["ticks", integer_text(value.ticks), value.scale, value.timezone, value.offset_naive]
    if isinstance(value, (dt.datetime, pd.Timestamp)):
        timestamp = pd.Timestamp(value)
        return ["datetime", timestamp.isoformat(), str(timestamp.tz) if timestamp.tz else None]
    if isinstance(value, dt.date):
        return ["date", value.isoformat()]
    if isinstance(value, UUID):
        return ["uuid", str(value)]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ["bytes", base64.b64encode(bytes(value)).decode("ascii")]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, (OrderedMap, OrderedMapPairs)):
        return ["map_pairs", [[encode_value(k), encode_value(v)] for k, v in value.pairs]]
    if isinstance(value, dict):
        return ["map", [[encode_value(k), encode_value(v)] for k, v in value.items()]]
    if isinstance(value, (list, tuple)):
        return ["tuple" if isinstance(value, tuple) else "array", [encode_value(v) for v in value]]
    msg = f"Unsupported exact replay value type: {type(value).__name__}"
    raise TypeError(msg)


def _decoded_map(pairs: tuple[tuple[Any, Any], ...], *, ordered: bool) -> Any:
    if ordered:
        return OrderedMapPairs(pairs)
    try:
        mapping = dict(pairs)
    except (TypeError, ValueError):
        return OrderedMapPairs(pairs)
    return mapping if len(mapping) == len(pairs) else OrderedMapPairs(pairs)


def decode_value(encoded: Any) -> Any:  # noqa: C901, PLR0911 - tagged scalar codec
    tag, *payload = encoded
    if tag == "null":
        return None
    if tag in {"bool", "str"}:
        return payload[0]
    if tag == "int":
        return _parse_integer_text(payload[0])
    if tag == "float":
        return float.fromhex(payload[0])
    if tag == "decimal":
        return Decimal(payload[0])
    if tag == "ticks":
        return ExactTemporal(
            _parse_integer_text(payload[0]),
            int(payload[1]),
            payload[2],
            bool(payload[3]) if len(payload) > _LEGACY_TICKS_FIELDS else False,
        )
    if tag == "datetime":
        value = pd.Timestamp(payload[0])
        return value.tz_convert(payload[1]) if payload[1] else value
    if tag == "date":
        return dt.date.fromisoformat(payload[0])
    if tag == "uuid":
        return UUID(payload[0])
    if tag == "bytes":
        return base64.b64decode(payload[0], validate=True)
    if tag in {"array", "tuple"}:
        values = [decode_value(value) for value in payload[0]]
        return tuple(values) if tag == "tuple" else values
    if tag in {"map", "map_pairs"}:
        pairs = tuple((decode_value(k), decode_value(v)) for k, v in payload[0])
        return _decoded_map(pairs, ordered=tag == "map_pairs")
    msg = "Unknown exact-value encoding tag."
    raise ValueError(msg)


def typed_text(  # noqa: C901, PLR0911 - scalar-family encoding
    value: Any,
    *,
    source_backend: str = "",
    source_type: str = "",
    fractional_scale: int | None = None,
) -> str | None:
    """Stable, exact fallback. Container keys are encoded as ordered pairs."""
    value = unwrap_enum(value)
    if is_null(value):
        return None
    if isinstance(value, Integral) and not isinstance(value, bool):
        return integer_text(int(value))
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, ExactTemporal):
        return value.isoformat(utc=True)
    if isinstance(value, (dt.datetime, pd.Timestamp)):
        timestamp = pd.Timestamp(value)
        if timestamp.tz is not None:
            timestamp = timestamp.tz_convert("UTC")
        if fractional_scale is not None:
            date_part = timestamp.date().isoformat() + timestamp.strftime("T%H:%M:%S")
            fraction = f"{timestamp.microsecond * 1000 + timestamp.nanosecond:09d}"[
                :fractional_scale
            ]
            return str(
                date_part
                + ("." + fraction if fractional_scale else "")
                + ("+00:00" if timestamp.tz else "")
            )
        return str(timestamp.isoformat())
    if isinstance(value, (dt.date, UUID)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "base64:" + base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, str):
        return value
    return json.dumps(
        {
            "v": ENCODING_VERSION,
            "backend": source_backend,
            "type": source_type,
            "value": encode_value(value),
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
