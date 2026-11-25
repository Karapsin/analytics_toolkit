"""Native Greenplum clock/interval precision without losing calendar components."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from math import gcd
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.backends.native_scalars import ExactArray, ExactInterval

if TYPE_CHECKING:
    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
    from analytics_toolkit.sql.core.exact_values import ExactClock

_SECOND = 10**9
_DAY_SECONDS = 86400
_DAY = _DAY_SECONDS * _SECOND
_MAX_PRECISION = 6
_OFFSET_LIMIT = 16 * 3600
_MINUTE_UNITS = 60
_INTERVAL_FIELDS = {
    "",
    "year",
    "month",
    "day",
    "hour",
    "minute",
    "second",
    "year to month",
    "day to hour",
    "day to minute",
    "day to second",
    "hour to minute",
    "hour to second",
    "minute to second",
}


class TimeTransport(str):
    """A certified clock literal, including PostgreSQL's valid 24:00 boundary."""

    __slots__ = ()


class IntervalTransport(str):
    """A certified literal retaining independent days and time components."""

    __slots__ = ()


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


@dataclass(frozen=True)
class _TemporalType:
    family: str
    precision: int
    with_timezone: bool = False
    least_field: str = "second"


@lru_cache(maxsize=1024)
def _temporal_type(native_type: str) -> _TemporalType | None:
    from .type_identity import canonical_builtin_type  # noqa: PLC0415 -- backend native grammar.

    native = " ".join((canonical_builtin_type(native_type) or native_type).lower().split())
    clock = re.fullmatch(
        r"(time|timetz)\s*(?:\(\s*(\d+)\s*\))?(?: (with|without) time zone)?", native
    )
    if clock:
        name, precision, awareness = clock.groups()
        return _TemporalType(
            "time",
            int(precision) if precision else _MAX_PRECISION,
            with_timezone=name == "timetz" or awareness == "with",
        )
    prefix_interval = re.fullmatch(r"interval\s*\(\s*(\d+)\s*\) ([a-z ]+)", native)
    if prefix_interval and prefix_interval[2] in _INTERVAL_FIELDS:
        # Bare SQL grammar permits precision before any field range. For ranges
        # ending before SECOND it is legal but has no fractional field to round.
        return _TemporalType(
            "interval", int(prefix_interval[1]), least_field=prefix_interval[2].split()[-1]
        )
    interval = re.fullmatch(r"interval(?: ([a-z ]+))?\s*(?:\(\s*(\d+)\s*\))?", native)
    if interval and (interval.group(1) or "").strip() in _INTERVAL_FIELDS:
        fields, precision = interval.groups()
        fields = fields.strip() if fields else ""
        least = fields.split()[-1] if fields else "second"
        if precision is not None and least != "second":
            message = "Greenplum INTERVAL precision requires a SECOND field."
            raise ValueError(message)
        return _TemporalType(
            "interval", int(precision) if precision else _MAX_PRECISION, least_field=least
        )
    return None


def is_gp_clock_interval_type(native_type: str) -> bool:
    return _temporal_type(native_type) is not None


def temporal_family(value: Any) -> str | None:
    if isinstance(value, (dt.time, TimeTransport)) or _exact_clock(value) is not None:
        return "time"
    return (
        "interval" if isinstance(value, (dt.timedelta, IntervalTransport, ExactInterval)) else None
    )


def _exact_clock(value: Any) -> ExactClock | None:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- registry cycle.
        ExactClock,
    )

    return value if isinstance(value, ExactClock) else None


def _exact_clock_offset(value: ExactClock) -> int | None:
    offset = value.offset_minutes * _MINUTE_UNITS if value.offset_minutes is not None else None
    if offset is not None and abs(offset) >= _OFFSET_LIMIT:
        message = "Greenplum TIME offset exceeds its native range."
        raise ValueError(message)
    return offset


def _observe_exact_clock(profile: ColumnProfile, value: ExactClock) -> None:
    offset = _exact_clock_offset(value)
    scale = value.scale
    fraction = value.ticks % 10**scale
    while scale and fraction % 10 == 0:
        fraction //= 10
        scale -= 1
    profile.temporal_scale = max(profile.temporal_scale, scale)
    profile.aware |= offset is not None
    profile.naive |= offset is None


def _time_parts(value: Any) -> tuple[int, int | None]:
    if isinstance(value, dt.time):
        ticks = (
            (value.hour * 3600 + value.minute * 60 + value.second) * 10**6 + value.microsecond
        ) * 1000
        offset_delta = value.utcoffset()
        offset_us = (
            (
                (offset_delta.days * _DAY_SECONDS + offset_delta.seconds) * 10**6
                + offset_delta.microseconds
            )
            if offset_delta is not None
            else None
        )
        if offset_us is not None and (offset_us % 10**6 or abs(offset_us) >= _OFFSET_LIMIT * 10**6):
            message = "Greenplum TIME offset must be whole seconds within its native range."
            raise ValueError(message)
        return ticks, offset_us // 10**6 if offset_us is not None else None
    if isinstance(value, TimeTransport):
        match = re.fullmatch(
            r"(\d{2}):(\d{2}):(\d{2})\.(\d{6,9})(?:([+-])(\d{2}):(\d{2}):(\d{2}))?", value
        )
        if match:
            hour, minute, second, fraction, sign, zone_hour, zone_minute, zone_second = (
                match.groups()
            )
            ticks = (int(hour) * 3600 + int(minute) * 60 + int(second)) * _SECOND + int(
                fraction.ljust(9, "0")
            )
            offset_seconds = (
                (int(zone_hour) * 3600 + int(zone_minute) * 60 + int(zone_second))
                * (-1 if sign == "-" else 1)
                if sign
                else None
            )
            if (
                0 <= ticks <= _DAY
                and int(minute) < _MINUTE_UNITS
                and int(second) < _MINUTE_UNITS
                and (
                    offset_seconds is None
                    or (
                        int(zone_minute) < _MINUTE_UNITS
                        and int(zone_second) < _MINUTE_UNITS
                        and abs(offset_seconds) < _OFFSET_LIMIT
                    )
                )
            ):
                return ticks, offset_seconds
    message = "Selected input is incompatible with authoritative Greenplum TIME values."
    raise ValueError(message)


def _checked_interval_parts(days: int, ticks: int) -> tuple[int, int]:
    if not -(2**31) <= days < 2**31 or not -(2**63) * 1000 <= ticks < 2**63 * 1000:
        message = "Greenplum INTERVAL components exceed their native range."
        raise ValueError(message)
    return days, ticks


def _interval_parts(value: Any) -> tuple[int, int]:
    if isinstance(value, ExactInterval):
        if not -(2**31) <= value.months < 2**31:
            message = "Greenplum INTERVAL months exceed their native range."
            raise ValueError(message)
        return _checked_interval_parts(value.days, value.microseconds * 1000)
    if isinstance(value, dt.timedelta):
        return _checked_interval_parts(
            value.days,
            (
                (value.seconds * 10**6 + value.microseconds) * 1000
                + int(getattr(value, "nanoseconds", 0))
            ),
        )
    if isinstance(value, IntervalTransport):
        match = re.fullmatch(
            r"(?:[+-]?\d+ mons )?([+-]?\d+) days ([+-]?\d+)\.(\d{6,9}) seconds", value
        )
        if match:
            days, second, fraction = match.groups()
            ticks = abs(int(second)) * _SECOND + int(fraction.ljust(9, "0"))
            return _checked_interval_parts(int(days), -ticks if second.startswith("-") else ticks)
    message = "Selected input is incompatible with authoritative Greenplum INTERVAL values."
    raise ValueError(message)


def _interval_months(value: Any) -> int:
    if isinstance(value, ExactInterval):
        return value.months
    if isinstance(value, IntervalTransport):
        match = re.match(r"([+-]?\d+) mons ", value)
        months = int(match[1]) if match else 0
        if not -(2**31) <= months < 2**31:
            message = "Greenplum INTERVAL months exceed their native range."
            raise ValueError(message)
        return months
    return 0


def _fraction_scale(ticks: int) -> int:
    fraction = abs(ticks) % _SECOND
    scale = 9
    while scale and fraction % 10 == 0:
        fraction //= 10
        scale -= 1
    return scale


def observe_temporal_value(profile: ColumnProfile, value: Any) -> None:
    """Accumulate exact native facts; never retain an input value in a profile."""
    try:
        clock = _exact_clock(value)
        if clock is not None:
            _observe_exact_clock(profile, clock)
        elif temporal_family(value) == "time":
            ticks, offset = _time_parts(value)
            profile.temporal_scale = max(profile.temporal_scale, _fraction_scale(ticks))
            profile.aware |= offset is not None
            profile.naive |= offset is None
            profile.time_zone_unresolved |= (
                isinstance(value, dt.time) and value.tzinfo is not None and offset is None
            )
        else:
            days, ticks = _interval_parts(value)
            profile.temporal_scale = max(profile.temporal_scale, _fraction_scale(ticks))
            profile.interval_days_nonzero |= bool(days)
            profile.interval_time_gcd = gcd(profile.interval_time_gcd, ticks)
            months = _interval_months(value)
            profile.interval_months_nonzero |= bool(months)
            profile.interval_months_gcd = gcd(profile.interval_months_gcd, months)
    except (TypeError, ValueError, OverflowError):
        profile.native_constraints_valid = False


def infer_clock_interval_type(profile: ColumnProfile) -> str | None:
    """Use native microsecond types only when the complete observed data fits."""
    if not profile.native_constraints_valid or profile.temporal_scale > _MAX_PRECISION:
        return None
    if profile.families == {"time"} and not (
        (profile.aware and profile.naive) or profile.time_zone_unresolved
    ):
        return "TIME(6) WITH TIME ZONE" if profile.aware else "TIME(6)"
    return "INTERVAL(6)" if profile.families == {"interval"} else None


def validate_clock_interval_profile(
    profile: ColumnProfile,
    native_type: str,
    *,
    allow_timestamp_precision_loss: bool = True,
) -> bool:
    spec = _temporal_type(native_type)
    if spec is None:
        return False
    if not 0 <= spec.precision <= _MAX_PRECISION:
        message = "Greenplum TIME/INTERVAL precision must be between 0 and 6."
        raise ValueError(message)
    if not profile.families <= {spec.family} or not profile.native_constraints_valid:
        message = (
            "Selected input is incompatible with authoritative Greenplum "
            f"{spec.family.upper()} values."
        )
        raise ValueError(message)
    if allow_timestamp_precision_loss:
        return True
    incompatible = profile.temporal_scale > spec.precision
    if spec.family == "interval":
        unit = {"day": _DAY, "hour": 3600 * _SECOND, "minute": 60 * _SECOND}.get(spec.least_field)
        if spec.least_field in {"year", "month"}:
            incompatible = profile.interval_days_nonzero or bool(profile.interval_time_gcd)
            if spec.least_field == "year":
                incompatible |= bool(profile.interval_months_gcd % 12)
        elif unit is not None:
            incompatible = (
                bool(profile.interval_time_gcd % unit)
                if spec.least_field != "day"
                else bool(profile.interval_time_gcd)
            )
    if incompatible:
        message = (
            "Selected input exceeds authoritative Greenplum TIME/INTERVAL precision or fields."
        )
        raise ValueError(message)
    return True


def _round_ticks(ticks: int, unit: int) -> int:
    return (1 if ticks >= 0 else -1) * ((abs(ticks) + unit // 2) // unit) * unit


def _time_text(ticks: int, offset: int | None, *, digits: int) -> TimeTransport:
    seconds, fraction = divmod(ticks, _SECOND)
    hour, remainder = divmod(seconds, 3600)
    minute, second = divmod(remainder, 60)
    suffix = ""
    if offset is not None:
        zone_hour, remainder = divmod(abs(offset), 3600)
        zone_minute, zone_second = divmod(remainder, 60)
        suffix = f"{'-' if offset < 0 else '+'}{zone_hour:02d}:{zone_minute:02d}:{zone_second:02d}"
    fraction_text = f"{fraction:09d}"[:digits]
    return TimeTransport(f"{hour:02d}:{minute:02d}:{second:02d}.{fraction_text}{suffix}")


def _interval_text(days: int, ticks: int, *, digits: int, months: int = 0) -> IntervalTransport:
    seconds, fraction = divmod(abs(ticks), _SECOND)
    fraction_text = f"{fraction:09d}"[:digits]
    return IntervalTransport(
        (f"{months} mons " if months else "")
        + f"{days} days {'-' if ticks < 0 else '+'}{seconds}.{fraction_text} seconds"
    )


def _snapshot_time(value: dt.time) -> dt.time:
    from analytics_toolkit.sql.core.temporal_zones import (  # noqa: PLC0415 -- registry cycle.
        canonical_named_zone,
    )

    zone = value.tzinfo
    if zone is not None:
        key = getattr(zone, "key", None) or getattr(zone, "zone", None)
        # Freeze the Python value before choosing a destination. Values outside
        # GP's offset domain may still have an exact typed-text representation.
        captured = value.utcoffset()
        snapshot: dt.tzinfo = _ClockZoneSnapshot(captured, value.tzname(), key)
        if isinstance(key, str):
            try:
                candidate = canonical_named_zone(key)
            except ValueError:
                pass  # Native offsets still fit; the exact text codec rejects unknown names.
            else:
                candidate_clock = value.replace(tzinfo=candidate)
                if (
                    candidate_clock.utcoffset() == captured
                    and candidate_clock.tzname() == snapshot.tzname(None)
                ):
                    snapshot = candidate
        zone = snapshot
    return dt.time(
        value.hour, value.minute, value.second, value.microsecond, tzinfo=zone, fold=value.fold
    )


def _freeze_native_array(value: Any, native: str) -> Any:
    from analytics_toolkit.sql.backends.nested_values import array_item  # noqa: PLC0415
    from analytics_toolkit.sql.core.exact_values import is_null  # noqa: PLC0415 -- registry cycle.

    value = array_item(value)
    if is_null(value):
        return None
    if isinstance(value, (list, tuple)):
        return [_freeze_native_array(item, native) for item in value]
    return freeze_clock_interval_value(value, native)


def freeze_clock_interval_value(value: Any, native_type: str) -> Any:
    """Own exact components before validation; do not round an existing schema."""
    from analytics_toolkit.sql.backends.nested_values import array_item  # noqa: PLC0415
    from analytics_toolkit.sql.core.exact_values import is_null  # noqa: PLC0415 -- registry cycle.

    from .type_identity import canonical_builtin_type  # noqa: PLC0415 -- native identity.

    value = array_item(value)
    if is_null(value):
        return None
    native = (canonical_builtin_type(native_type) or native_type).rstrip()
    if native.endswith("[]"):
        return _freeze_clock_interval_array(value, native)
    spec = _temporal_type(native)
    if spec is None:
        message = "A supported Greenplum TIME/INTERVAL destination is required."
        raise ValueError(message)
    if spec.family == "time":
        return _freeze_clock(value)
    _interval_parts(value)
    return value  # Native durations are immutable; retain their exact type/unit for text fallback.


def _freeze_clock_interval_array(value: Any, native: str) -> Any:
    while native.endswith("[]"):
        native = native[:-2].rstrip()
    if isinstance(value, ExactArray):
        return replace(
            value, values=tuple(freeze_clock_interval_value(item, native) for item in value.values)
        )
    if isinstance(value, (list, tuple)):
        return _freeze_native_array(value, native)
    message = "Greenplum native temporal arrays require array values."
    raise ValueError(message)


def _freeze_clock(value: Any) -> Any:
    if _exact_clock(value) is not None:
        return value  # Immutable coordinates preserve sub-microticks for exact text fallback.
    if isinstance(value, dt.time):
        return _snapshot_time(value)
    return _time_text(*_time_parts(value), digits=9)


def convert_clock_interval_value(value: Any, native_type: str) -> Any:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- registry cycle.
        is_null,
        unwrap_enum,
    )

    value = unwrap_enum(value)
    if is_null(value):
        return None
    spec = _temporal_type(native_type)
    if spec is None or not 0 <= spec.precision <= _MAX_PRECISION:
        message = "A supported Greenplum TIME/INTERVAL destination is required."
        raise ValueError(message)
    unit = 10 ** (9 - spec.precision)
    if spec.family == "time":
        clock = _exact_clock(value)
        if clock is not None:
            offset = _exact_clock_offset(clock)
            if clock.scale >= spec.precision:
                clock_unit = 10 ** (clock.scale - spec.precision)
                rounded = _round_ticks(clock.ticks, clock_unit) // clock_unit
            else:
                rounded = clock.ticks * 10 ** (spec.precision - clock.scale)
            micros = rounded * 10 ** (_MAX_PRECISION - spec.precision)
            return _time_text(micros * 1000, offset if spec.with_timezone else None, digits=6)
        ticks, offset = _time_parts(value)
        return _time_text(
            _round_ticks(ticks, unit), offset if spec.with_timezone else None, digits=6
        )
    days, ticks = _interval_parts(value)
    months = _interval_months(value)
    if spec.least_field in {"year", "month"}:
        days, ticks = 0, 0
        if spec.least_field == "year":
            months = (1 if months >= 0 else -1) * (abs(months) // 12) * 12
    elif spec.least_field == "day":
        ticks = 0
    elif spec.least_field in {"hour", "minute"}:
        field_unit = (3600 if spec.least_field == "hour" else 60) * _SECOND
        ticks = (1 if ticks >= 0 else -1) * (abs(ticks) // field_unit) * field_unit
    else:
        ticks = _round_ticks(ticks, unit)
    _checked_interval_parts(days, ticks)
    return _interval_text(days, ticks, digits=6, months=months)
