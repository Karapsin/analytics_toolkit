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
MAX_EXACT_SCALE = 12
_PYTHON_TEMPORAL_SCALE = 6
_INTEGER_CHUNK_DIGITS = 1000
_INTEGER_CHUNK_BASE = 10**_INTEGER_CHUNK_DIGITS
_LEGACY_TICKS_FIELDS = 3
_WALL_TICKS_FIELDS = 4
_DAY_SECONDS = 86400
_DAY_MINUTES = 1440


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
    wall_time: bool = False

    def ticks_floor(self, scale: int) -> int:
        """Return integer range coordinates; original ticks retain precision proof."""
        return _rescale_ticks(self.ticks, self.scale, scale)

    def timestamp(self) -> Any:
        _validate_exact_scale(self.scale)
        ticks, scale = self.ticks, self.scale
        while scale and ticks % 10 == 0:
            ticks //= 10
            scale -= 1
        if scale > MAX_TEMPORAL_SCALE:
            return self
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
        _validate_exact_scale(self.scale)
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
            suffix = suffix[:3] + ":" + suffix[3:5] + (":" + suffix[5:] if suffix[5:] else "")
        return base + (f".{fraction:0{self.scale}d}" if self.scale else "") + suffix


def _validate_exact_scale(scale: int) -> None:
    if not 0 <= scale <= MAX_EXACT_SCALE:
        msg = "Exact temporal scale must be between zero and twelve."
        raise ValueError(msg)


def _rescale_ticks(ticks: int, source_scale: int, target_scale: int) -> int:
    _validate_exact_scale(source_scale)
    _validate_exact_scale(target_scale)
    factor: int = 10 ** abs(source_scale - target_scale)
    if source_scale > target_scale:
        return ticks // factor
    return ticks * factor


@dataclass(frozen=True)
class ExactClock:
    """Date-free native clock coordinates, including sub-microsecond fractions."""

    ticks: int
    scale: int
    offset_minutes: int | None = None
    timezone_name: str | None = None

    def __post_init__(self) -> None:
        _validate_exact_scale(self.scale)
        if not 0 <= self.ticks < _DAY_SECONDS * 10**self.scale:
            msg = "Exact clock ticks must identify a time within one day."
            raise ValueError(msg)
        if (
            self.offset_minutes is not None
            and not -_DAY_MINUTES < self.offset_minutes < _DAY_MINUTES
        ):
            msg = "Exact clock UTC offset must be within one day."
            raise ValueError(msg)
        if self.offset_minutes is None and self.timezone_name is not None:
            msg = "An exact clock timezone name requires a fixed UTC offset."
            raise ValueError(msg)

    def ticks_floor(self, scale: int) -> int:
        return _rescale_ticks(self.ticks, self.scale, scale)

    def time(self) -> dt.time | ExactClock:
        if self.scale > _PYTHON_TEMPORAL_SCALE and self.ticks % 10 ** (
            self.scale - _PYTHON_TEMPORAL_SCALE
        ):
            return self
        micros = self.ticks_floor(_PYTHON_TEMPORAL_SCALE)
        seconds, fraction = divmod(micros, 10**_PYTHON_TEMPORAL_SCALE)
        hour, remainder = divmod(seconds, 3600)
        minute, second = divmod(remainder, 60)
        zone = None
        if self.offset_minutes is not None:
            offset = dt.timedelta(minutes=self.offset_minutes)
            zone = (
                dt.timezone(offset, self.timezone_name)
                if self.timezone_name is not None
                else dt.timezone(offset)
            )
        return dt.time(hour, minute, second, fraction, tzinfo=zone)

    def isoformat(self) -> str:
        seconds, fraction = divmod(self.ticks, 10**self.scale)
        hour, remainder = divmod(seconds, 3600)
        minute, second = divmod(remainder, 60)
        result = f"{hour:02d}:{minute:02d}:{second:02d}"
        if self.scale:
            result += f".{fraction:0{self.scale}d}"
        if self.offset_minutes is not None:
            hours, minutes = divmod(abs(self.offset_minutes), 60)
            sign = "-" if self.offset_minutes < 0 else "+"
            result += f"{sign}{hours:02d}:{minutes:02d}"
        return result


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
        return ExactTemporal(ticks, scale, value.timezone, value.offset_naive, value.wall_time)
    if isinstance(value, pd.Timestamp) and scale <= _PYTHON_TEMPORAL_SCALE:
        value = value.to_pydatetime(warn=False)
    if isinstance(value, dt.datetime) and scale < _PYTHON_TEMPORAL_SCALE:
        factor = 10 ** (_PYTHON_TEMPORAL_SCALE - scale)
        return value.replace(microsecond=(value.microsecond // factor) * factor)
    if isinstance(value, pd.Timestamp) and scale < MAX_TEMPORAL_SCALE:
        factor = 10 ** (MAX_TEMPORAL_SCALE - scale)
        return value.replace(nanosecond=(value.nanosecond // factor) * factor)
    return value


def _encode_time_zone(
    zone: dt.tzinfo | None, wall: dt.time, offset: dt.timedelta | None, label: str | None
) -> Any:
    if zone is None:
        return None
    name = getattr(zone, "key", None) or getattr(zone, "zone", None)
    if name is not None:
        candidate = wall.replace(tzinfo=canonical_named_zone(name))
        if candidate.utcoffset() == offset and candidate.tzname() == label:
            return ["named", name]
    if offset is None:
        msg = "Clock timezone cannot be replayed without inventing a date."
        raise TypeError(msg)
    return ["fixed", offset.days, offset.seconds, offset.microseconds, label]


def _encode_clock(value: dt.time) -> list[Any]:
    offset, label = value.utcoffset(), value.tzname()
    wall = dt.time(value.hour, value.minute, value.second, value.microsecond, fold=value.fold)
    descriptor = _encode_time_zone(value.tzinfo, wall, offset, label)
    if offset is not None:
        zone = dt.timezone(offset, label) if label is not None else dt.timezone(offset)
        wall = wall.replace(tzinfo=zone)
    return ["time", wall.isoformat(), descriptor, value.fold]


def _decode_time(payload: Any) -> dt.time:
    value = dt.time.fromisoformat(payload[0])
    descriptor = payload[1]
    zone = None
    if descriptor is not None:
        if descriptor[0] == "named":
            zone = canonical_named_zone(descriptor[1])
        elif descriptor[0] == "fixed":
            offset = dt.timedelta(
                days=descriptor[1], seconds=descriptor[2], microseconds=descriptor[3]
            )
            zone = (
                dt.timezone(offset, descriptor[4])
                if descriptor[4] is not None
                else dt.timezone(offset)
            )
        else:
            msg = "Unknown exact clock timezone encoding."
            raise ValueError(msg)
    return value.replace(tzinfo=zone, fold=int(payload[2]))


def _decode_calendar_value(tag: str, payload: Any) -> Any:
    if tag == "date":
        return dt.date.fromisoformat(payload[0])
    if tag == "time":
        return _decode_time(payload)
    if tag == "clock_ticks":
        return ExactClock(_parse_integer_text(payload[0]), int(payload[1]), payload[2], payload[3])
    if tag == "pandas_timedelta":
        return pd.Timedelta(np.timedelta64(_parse_integer_text(payload[0]), payload[1]))
    return dt.timedelta(days=payload[0], seconds=payload[1], microseconds=payload[2])


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
        payload = [
            "ticks",
            integer_text(value.ticks),
            value.scale,
            value.timezone,
            value.offset_naive,
        ]
        return [*payload, True] if value.wall_time else payload
    if isinstance(value, ExactClock):
        return [
            "clock_ticks",
            integer_text(value.ticks),
            value.scale,
            value.offset_minutes,
            value.timezone_name,
        ]
    if isinstance(value, (dt.datetime, pd.Timestamp)):
        timestamp = pd.Timestamp(value)
        return ["datetime", timestamp.isoformat(), str(timestamp.tz) if timestamp.tz else None]
    if isinstance(value, dt.date):
        return ["date", value.isoformat()]
    if isinstance(value, dt.time):
        return _encode_clock(value)
    if isinstance(value, pd.Timedelta):
        raw = value.asm8
        return [
            "pandas_timedelta",
            integer_text(int(raw.view(np.int64))),
            np.datetime_data(raw.dtype)[0],
        ]
    if isinstance(value, dt.timedelta):
        return ["timedelta", value.days, value.seconds, value.microseconds]
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
            bool(payload[4]) if len(payload) > _WALL_TICKS_FIELDS else False,
        )
    if tag == "datetime":
        value = pd.Timestamp(payload[0])
        return value.tz_convert(payload[1]) if payload[1] else value
    if tag in {"date", "time", "clock_ticks", "pandas_timedelta", "timedelta"}:
        return _decode_calendar_value(tag, payload)
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
