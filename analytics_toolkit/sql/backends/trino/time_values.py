"""Freeze native Trino clocks and retain their exact server cast semantics."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pandas as pd

    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
    from analytics_toolkit.sql.core.exact_values import ExactClock

_MINUTE_MICROSECONDS = 60 * 10**6
_OFFSET_LIMIT = 14 * 60 * _MINUTE_MICROSECONDS
_MAX_PRECISION = 12
_NATIVE_PRECISION = 6


@dataclass(frozen=True)
class _TimeType:
    precision: int
    with_timezone: bool = False


@dataclass(frozen=True)
class _ClockZoneSnapshot(dt.tzinfo):
    offset: dt.timedelta | None
    name: str | None
    key: str | None = None

    def utcoffset(self, _value: dt.datetime | None) -> dt.timedelta | None:
        return self.offset

    def tzname(self, _value: dt.datetime | None) -> str | None:
        return self.name

    def dst(self, _value: dt.datetime | None) -> dt.timedelta | None:
        return None


@lru_cache(maxsize=1024)
def _time_type(native_type: str) -> _TimeType | None:
    match = re.fullmatch(
        r"time\s*(?:\(\s*(\d+)\s*\))?(?:\s+(with|without)\s+time\s+zone)?",
        native_type.strip(),
        flags=re.IGNORECASE,
    )
    return (
        _TimeType(
            int(match[1]) if match[1] is not None else 3,
            with_timezone=(match[2] or "").lower() == "with",
        )
        if match
        else None
    )


def is_time_type(native_type: str) -> bool:
    return _time_type(native_type) is not None


def _checked_type(native_type: str) -> _TimeType:
    spec = _time_type(native_type)
    if spec is None or not 0 <= spec.precision <= _MAX_PRECISION:
        message = "A Trino TIME precision between 0 and 12 is required."
        raise ValueError(message)
    return spec


def _offset(value: dt.time) -> dt.timedelta | None:
    offset = value.utcoffset()
    if value.tzinfo is not None and offset is None:
        message = (
            "Trino TIME cannot resolve a named timezone without a date. "
            "Supply a fixed UTC offset or a timestamp with an explicit date/timezone policy."
        )
        raise ValueError(message)
    if offset is not None:
        micros = (offset.days * 86400 + offset.seconds) * 10**6 + offset.microseconds
        if micros % _MINUTE_MICROSECONDS or abs(micros) > _OFFSET_LIMIT:
            message = "Trino TIME offsets must be whole minutes within -14:00 and +14:00."
            raise ValueError(message)
    return offset


def freeze_time_value(value: Any) -> dt.time | ExactClock:
    """Capture a clock before choosing native TIME or exact typed text."""
    from analytics_toolkit.sql.core.exact_values import ExactClock, unwrap_enum  # noqa: PLC0415
    from analytics_toolkit.sql.core.temporal_zones import canonical_named_zone  # noqa: PLC0415

    value = unwrap_enum(value)
    if isinstance(value, ExactClock):
        return value
    if isinstance(value, str):
        from .exact_temporal_reads import decode_time  # noqa: PLC0415 -- exact text input boundary.

        token = value.strip()
        with_timezone = re.search(r"[+-][0-9]{2}:[0-9]{2}$", token, flags=re.ASCII) is not None
        decoded = decode_time(token, _MAX_PRECISION, with_timezone=with_timezone)
        if decoded is None:
            message = "Selected input is incompatible with authoritative Trino TIME values."
            raise TypeError(message)
        return decoded
    if not isinstance(value, dt.time):
        message = "Selected input is incompatible with authoritative Trino TIME values."
        raise TypeError(message)
    zone = value.tzinfo
    if zone is not None and not isinstance(zone, (dt.timezone, _ClockZoneSnapshot)):
        offset, name = value.utcoffset(), value.tzname()
        key = getattr(zone, "key", None) or getattr(zone, "zone", None)
        snapshot: dt.tzinfo = _ClockZoneSnapshot(offset, name, key)
        if isinstance(key, str):
            try:
                candidate = canonical_named_zone(key)
            except ValueError:
                pass  # The text codec rejects unverified named zones as before.
            else:
                candidate_clock = value.replace(tzinfo=candidate)
                if candidate_clock.utcoffset() == offset and candidate_clock.tzname() == name:
                    snapshot = candidate
        zone = snapshot
    return dt.time(
        value.hour, value.minute, value.second, value.microsecond, tzinfo=zone, fold=value.fold
    )


def _snapshot_time(value: Any) -> dt.time | ExactClock:
    from analytics_toolkit.sql.core.exact_values import ExactClock  # noqa: PLC0415

    value = freeze_time_value(value)
    if isinstance(value, ExactClock):
        if not 0 <= value.ticks < 86400 * 10**value.scale:
            message = (
                "Trino TIME requires clock coordinates before 24:00; "
                "raw 24:00 cannot be wrapped to midnight."
            )
            raise ValueError(message)
        if value.offset_minutes is not None and abs(value.offset_minutes) > 14 * 60:
            message = "Trino TIME offsets must be whole minutes within -14:00 and +14:00."
            raise ValueError(message)
        ordinary = value.time()
        return value if isinstance(ordinary, ExactClock) else _snapshot_time(ordinary)
    offset = _offset(value)
    zone = None
    if offset is not None:
        minutes = (offset.days * 86400 + offset.seconds) // 60
        hours, remaining = divmod(abs(minutes), 60)
        # The SDK slices the first three characters of tzname. Name UTC+00:00
        # explicitly too: the default UTC name would erase a genuine zero offset.
        name = f"UTC{'-' if minutes < 0 else '+'}{hours:02d}:{remaining:02d}"
        zone = dt.timezone(offset, name)
    return dt.time(
        value.hour, value.minute, value.second, value.microsecond, tzinfo=zone, fold=value.fold
    )


def _clock_coordinates(value: dt.time | ExactClock) -> ExactClock:
    from analytics_toolkit.sql.core.exact_values import ExactClock  # noqa: PLC0415

    if isinstance(value, ExactClock):
        return value
    offset = value.utcoffset()
    minutes = None if offset is None else (offset.days * 86400 + offset.seconds) // 60
    ticks = (value.hour * 3600 + value.minute * 60 + value.second) * 10**6 + value.microsecond
    return ExactClock(ticks, _NATIVE_PRECISION, minutes, value.tzname())


def observe_time_value(profile: ColumnProfile, value: Any) -> None:
    """Record backend constraints without retaining values or inventing a date."""
    try:
        _snapshot_time(value)
    except (TypeError, ValueError, OverflowError):
        profile.trino_time_constraints_valid = False


def validate_time_profile(
    profile: ColumnProfile,
    native_type: str,
    *,
    allow_timestamp_precision_loss: bool = True,
) -> bool:
    contains_time_type(native_type)  # Validate declarations even for empty nested inputs.
    if not is_time_type(native_type):
        return False
    spec = _checked_type(native_type)
    if not profile.families <= {"time"} or not profile.trino_time_constraints_valid:
        message = (
            "Selected input is incompatible with authoritative Trino TIME values; "
            "use native time values with a fixed whole-minute UTC offset within ±14:00."
        )
        raise ValueError(message)
    if not allow_timestamp_precision_loss and profile.temporal_scale > spec.precision:
        message = "Selected input exceeds authoritative Trino TIME precision."
        raise ValueError(message)
    return True


def convert_time_value(value: Any, native_type: str) -> dt.time | ExactClock | None:
    """Round intentional precision conversions; leave timezone casts to Trino."""
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415
        ExactClock,
        is_null,
        unwrap_enum,
    )

    spec = _checked_type(native_type)
    value = unwrap_enum(value)
    if is_null(value):
        return None
    if isinstance(value, str):
        # Load the wire codec only at the native text-conversion boundary.
        from .exact_temporal_reads import (  # noqa: PLC0415
            decode_time,
        )

        value = decode_time(value.strip(), _MAX_PRECISION, with_timezone=spec.with_timezone)
    value = _snapshot_time(value)
    clock = _clock_coordinates(value)
    if clock.scale > spec.precision:
        unit = 10 ** (clock.scale - spec.precision)
        ticks = (clock.ticks + unit // 2) // unit
    else:
        ticks = clock.ticks_floor(spec.precision)
    ticks %= 86400 * 10**spec.precision
    converted = ExactClock(ticks, spec.precision, clock.offset_minutes, clock.timezone_name).time()
    if isinstance(converted, ExactClock):
        return converted
    converted = cast("dt.time", _snapshot_time(converted))
    return converted.replace(fold=value.fold if isinstance(value, dt.time) else 0)


def time_parameter_value(value: Any, native_type: str) -> Any:
    """Represent resolved clocks exactly for every supported SDK formatter."""
    from analytics_toolkit.sql.core.exact_values import ExactClock  # noqa: PLC0415

    spec = _checked_type(native_type)
    converted = convert_time_value(value, native_type)
    if converted is None:
        return None
    if spec.with_timezone and _clock_coordinates(converted).offset_minutes is None:
        # A typed TIME parameter invokes Trino's session-timezone cast. A
        # VARCHAR without an offset is invalid for TIME WITH TIME ZONE.
        return converted
    if not spec.with_timezone:
        return (
            ExactClock(converted.ticks, converted.scale).isoformat()
            if isinstance(converted, ExactClock)
            else converted.replace(tzinfo=None).isoformat()
        )
    return converted.isoformat()


def time_key_value_and_identity(
    value: Any, native_type: str | None
) -> tuple[bool, dt.time | ExactClock | None, int | None]:
    """Return a lossless key payload and its native Trino equality identity."""
    if not is_time_type(native_type or ""):
        return False, None, None
    native_type = native_type or ""
    spec = _checked_type(native_type)
    value = convert_time_value(value, native_type)
    if value is None:
        message = "Trino TIME map keys cannot be NULL."
        raise ValueError(message)
    clock = _clock_coordinates(value)
    identity_scale = max(_NATIVE_PRECISION, spec.precision)
    ticks = clock.ticks_floor(identity_scale)
    if spec.with_timezone:
        if clock.offset_minutes is None:
            message = (
                "Cannot certify Trino TIME WITH TIME ZONE map keys without a fixed UTC offset; "
                "the native cast uses the session timezone at query start."
            )
            raise ValueError(message)
        ticks -= clock.offset_minutes * 60 * 10**identity_scale
    ticks %= 86400 * 10**identity_scale
    if spec.with_timezone:
        # Python-equal aware keys are native-equal too. Retain their stored
        # offsets; the separate modulo-day identity detects the reverse case.
        return True, value, ticks
    from analytics_toolkit.sql.core.exact_values import ExactClock  # noqa: PLC0415

    return True, ExactClock(clock.ticks, clock.scale).time(), ticks


def time_key_identity(value: Any, native_type: str | None) -> tuple[bool, int | None]:
    """Match native key equality without relying on Python's aware clock hash."""
    handled, _value, identity = time_key_value_and_identity(value, native_type)
    return handled, identity


def contains_time_type(native_type: str) -> bool:
    from analytics_toolkit.sql.backends.nested_types import nested_descriptor  # noqa: PLC0415
    from analytics_toolkit.sql.backends.type_resolution import parse_native_type  # noqa: PLC0415

    family = parse_native_type(native_type).base.name
    if family in {"time", "time with time zone", "time without time zone"}:
        _checked_type(native_type)
        return True
    # Visit every declaration; a valid first field must not conceal an invalid
    # later clock precision when all observed rows are NULL or empty containers.
    kind, fields = nested_descriptor(native_type)
    children = [contains_time_type(child) for _name, child in fields]
    if kind in {"gp_array", "tuple"} and any(children):
        message = "Trino TIME containers require native ARRAY, MAP or ROW destination syntax."
        raise ValueError(message)
    return any(children)


def _freeze_row(value: Any, children: list[tuple[str, str]]) -> Any:
    if isinstance(value, dict):
        native_children = dict(children)
        return {
            name: _freeze_value(item, native_children[name]) if name in native_children else item
            for name, item in value.items()
        }
    if isinstance(value, (list, tuple)) and len(value) == len(children):
        return tuple(_freeze_value(item, native) for item, (_name, native) in zip(value, children))
    return value


def _freeze_nested_value(value: Any, native_type: str) -> Any:
    from analytics_toolkit.sql.backends.nested_types import nested_descriptor  # noqa: PLC0415
    from analytics_toolkit.sql.core.exact_values import OrderedMapPairs  # noqa: PLC0415

    kind, children = nested_descriptor(native_type)
    if kind == "array" and isinstance(value, (list, tuple)):
        return [_freeze_value(item, children[0][1]) for item in value]
    if kind in {"row", "tuple"}:
        return _freeze_row(value, children)
    if kind == "map" and hasattr(value, "items"):
        return OrderedMapPairs(
            tuple(
                (_freeze_value(key, children[0][1]), _freeze_value(item, children[1][1]))
                for key, item in value.items()
            )
        )
    return value


def _freeze_value(value: Any, native_type: str | None) -> Any:
    from analytics_toolkit.sql.backends.nested_values import array_item  # noqa: PLC0415
    from analytics_toolkit.sql.core.exact_values import is_null  # noqa: PLC0415

    value = array_item(value)
    if is_null(value):
        return None
    if native_type is None:
        return freeze_time_value(value)
    if is_time_type(native_type):
        return (
            convert_time_value(value, native_type)
            if isinstance(value, str)
            else _snapshot_time(value)
        )
    return _freeze_nested_value(value, native_type)


def prepare_dataframe_payload(
    self: Any, df: pd.DataFrame, schema: Mapping[str, str] | None
) -> pd.DataFrame | None:
    """Own timezone callbacks once before validation, mutation and data replay."""
    del self
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415
        ExactClock,
        is_null,
        unwrap_enum,
    )

    columns: list[tuple[int, Any, str | None]] = []
    if schema is None:
        for index, name in enumerate(df.columns):
            logical_values = (unwrap_enum(value) for value in df.iloc[:, index])
            values = [value for value in logical_values if not is_null(value)]
            if values and all(isinstance(value, (dt.time, ExactClock)) for value in values):
                columns.append((index, name, None))
    else:
        columns = [
            (index, name, schema[str(name)])
            for index, name in enumerate(df.columns)
            if str(name) in schema and contains_time_type(schema[str(name)])
        ]
    if not columns:
        return None
    import pandas as pd  # noqa: PLC0415 -- dataframe transport boundary.

    prepared = df.copy(deep=False)
    for index, name, native in columns:
        prepared[name] = pd.Series(
            [_freeze_value(value, native) for value in df.iloc[:, index]],
            index=df.index,
            dtype=object,
        )
    return prepared


def validate_staging_schema(self: Any, schema: Mapping[str, str], *, use_parquet: bool) -> None:
    """Reject unsupported Hive clock staging before clearing an existing target."""
    del self
    if not use_parquet:
        return
    if any(contains_time_type(native) for native in schema.values()):
        message = (
            "Trino Parquet/Hive staging does not support TIME. "
            "Use parameter insertion or an explicit VARCHAR column."
        )
        raise ValueError(message)
    from .arrow_types import hive_stage_type, require_supported_stage_type  # noqa: PLC0415

    for native in schema.values():
        require_supported_stage_type(hive_stage_type(native))
