"""Decode Trino's calendar wire values without discarding fractional digits."""

from __future__ import annotations

import datetime as dt
import re

import pandas as pd

from analytics_toolkit.sql.core.exact_values import ExactClock, ExactTemporal
from analytics_toolkit.sql.core.temporal_zones import canonical_named_zone, localize_wall

_MAX_PRECISION = 12
_PYTHON_PRECISION = 6
_PANDAS_PRECISION = 9
_MINUTES_PER_HOUR = 60
_MAX_OFFSET_MINUTES = 14 * _MINUTES_PER_HOUR
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
_TIMESTAMP = re.compile(
    r"([+-]?\d{4,})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d{1,12}))?(?: ([^\s]+))?\Z",
    re.ASCII,
)
_TIME = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,12}))?"
    r"(?: ?([+-]\d{2}:\d{2}|[A-Za-z][^\s]*))?\Z",
    re.ASCII,
)
_FIXED_ZONE = re.compile(r"[+-](\d{2}):(\d{2})\Z", re.ASCII)


def _check_precision(precision: int) -> None:
    if isinstance(precision, bool) or not isinstance(precision, int):
        message = "Trino temporal precision must be an integer between 0 and 12."
        raise TypeError(message)
    if not 0 <= precision <= _MAX_PRECISION:
        message = "Trino temporal precision must be between 0 and 12."
        raise ValueError(message)


def _fraction_digits(fraction: str | None, precision: int) -> str:
    digits = fraction or ""
    if any(digit != "0" for digit in digits[precision:]):
        message = "Trino temporal response exceeds its declared precision."
        raise ValueError(message)
    return digits[:precision].ljust(precision, "0")


def _calendar(match: re.Match[str], digits: str) -> dt.datetime:
    try:
        year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
        return dt.datetime.combine(
            dt.date(year, month, day),
            dt.time(
                hour,
                minute,
                second,
                int(digits[:_PYTHON_PRECISION].ljust(_PYTHON_PRECISION, "0")),
            ),
        )
    except (ValueError, OverflowError):
        message = (
            "Trino timestamp is outside the supported Python calendar or has invalid fields; "
            "project an ISO string in source SQL to preserve it."
        )
        raise ValueError(message) from None


def _wide_timestamp(match: re.Match[str], digits: str, precision: int) -> ExactTemporal:
    from analytics_toolkit.sql.backends.calendar_ticks import days_from_civil  # noqa: PLC0415

    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    try:
        dt.time(hour, minute, second)
        seconds = days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second
    except (ValueError, OverflowError):
        message = "Trino timestamp response has invalid Gregorian calendar fields."
        raise ValueError(message) from None
    zone = match[8]
    if zone is not None:
        if zone.startswith(("+", "-")):
            seconds -= _offset_minutes(zone) * 60
        elif zone not in {"UTC", "GMT", "Z", "Etc/UTC", "Etc/GMT"}:
            message = (
                "Trino expanded-year timestamps require UTC or a fixed offset; "
                "project UTC in source SQL for named timezone rules."
            )
            raise ValueError(message)
    return ExactTemporal(
        seconds * 10**precision + int("0" + digits), precision, zone, wall_time=zone is None
    )


def _offset_minutes(zone_name: str) -> int:
    fixed = _FIXED_ZONE.fullmatch(zone_name)
    if (
        fixed is None
        or int(fixed[1]) * _MINUTES_PER_HOUR + int(fixed[2]) > _MAX_OFFSET_MINUTES
        or int(fixed[2]) >= _MINUTES_PER_HOUR
    ):
        message = "Trino temporal response has an invalid fixed UTC offset."
        raise ValueError(message)
    minutes = int(fixed[1]) * _MINUTES_PER_HOUR + int(fixed[2])
    return -minutes if zone_name.startswith("-") else minutes


def _with_zone(wall: dt.datetime, zone_name: str) -> dt.datetime:
    if zone_name.startswith(("+", "-")):
        _offset_minutes(zone_name)
    try:
        return localize_wall(wall, canonical_named_zone(zone_name))
    except (ValueError, OverflowError):
        message = (
            "Trino timestamp timezone is unresolved, ambiguous or nonexistent; "
            "project UTC in source SQL to identify the exact instant."
        )
        raise ValueError(message) from None


def _epoch_ticks(wall: dt.datetime, digits: str, precision: int) -> int:
    try:
        whole = wall.replace(microsecond=0)
        instant = (
            whole.replace(tzinfo=dt.timezone.utc)
            if whole.tzinfo is None
            else whole.astimezone(dt.timezone.utc)
        )
        delta = instant - _EPOCH
    except (ValueError, OverflowError):
        message = (
            "Trino timestamp is outside the supported exact calendar; "
            "project an ISO string in source SQL to preserve it."
        )
        raise ValueError(message) from None
    seconds = delta.days * 86400 + delta.seconds
    factor: int = 10**precision
    return seconds * factor + int("0" + digits)


def _precise_result(
    wall: dt.datetime, digits: str, precision: int, zone_name: str | None
) -> pd.Timestamp | ExactTemporal:
    ticks = _epoch_ticks(wall, digits, precision)
    if precision <= _PANDAS_PRECISION:
        try:
            stamp = pd.Timestamp(
                ticks * 10 ** (_PANDAS_PRECISION - precision),
                unit="ns",
                tz="UTC" if wall.tzinfo is not None else None,
            )
            if stamp is not pd.NaT:
                return stamp.tz_convert(wall.tzinfo) if wall.tzinfo is not None else stamp
        except (ValueError, OverflowError):
            # A Python-supported calendar can fall outside pandas' nanosecond range.
            pass
    return ExactTemporal(ticks, precision, zone_name, wall_time=zone_name is None)


def decode_timestamp(
    value: str | None, precision: int, *, with_timezone: bool
) -> dt.datetime | pd.Timestamp | ExactTemporal | None:
    """Preserve native microseconds, pandas nanoseconds and raw picosecond ticks."""
    _check_precision(precision)
    if value is None:
        return None
    if not isinstance(value, str):
        message = "Trino timestamp response must be a calendar string."
        raise TypeError(message)
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        message = "Trino timestamp response has an unsupported calendar representation."
        raise ValueError(message)
    zone_name = match[8]
    if with_timezone != (zone_name is not None):
        message = "Trino timestamp response does not match its declared timezone family."
        raise ValueError(message)
    digits = _fraction_digits(match[7], precision)
    if not dt.MINYEAR <= int(match[1]) <= dt.MAXYEAR:
        return _wide_timestamp(match, digits, precision)
    wall = _calendar(match, digits)
    if zone_name is not None:
        wall = _with_zone(wall, zone_name)
        try:
            wall.astimezone(dt.timezone.utc)
        except (ValueError, OverflowError):
            return _wide_timestamp(match, digits, precision)
    if precision <= _PYTHON_PRECISION:
        return wall
    return _precise_result(wall, digits, precision, zone_name)


def decode_time(
    value: str | None, precision: int, *, with_timezone: bool
) -> dt.time | ExactClock | None:
    """Decode date-free wall ticks before the SDK can round their fractional tail."""
    _check_precision(precision)
    if value is None:
        return None
    if not isinstance(value, str):
        message = "Trino TIME response must be a clock string."
        raise TypeError(message)
    match = _TIME.fullmatch(value)
    if match is None:
        message = "Trino TIME response has an unsupported clock representation."
        raise ValueError(message)
    zone_name = match[5]
    if with_timezone != (zone_name is not None):
        message = "Trino TIME response does not match its declared timezone family."
        raise ValueError(message)
    offset = _offset_minutes(zone_name) if zone_name is not None else None
    digits = _fraction_digits(match[4], precision)
    try:
        hour, minute, second = (int(part) for part in match.groups()[:3])
        native = dt.time(
            hour,
            minute,
            second,
            int(digits[:_PYTHON_PRECISION].ljust(_PYTHON_PRECISION, "0")),
            tzinfo=dt.timezone(dt.timedelta(minutes=offset)) if offset is not None else None,
        )
    except ValueError:
        message = "Trino TIME response has invalid clock fields."
        raise ValueError(message) from None
    if not any(digit != "0" for digit in digits[_PYTHON_PRECISION:]):
        return native
    ticks = (hour * 3600 + minute * 60 + second) * 10**precision + int(digits)
    return ExactClock(ticks, precision, offset)
