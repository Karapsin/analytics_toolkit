"""Calendar presentation for exact ticks outside bounded datetime containers."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from .temporal_zones import canonical_named_zone

if TYPE_CHECKING:
    from .exact_values import ExactTemporal

_DAY_SECONDS = 86400
_MICROSECOND_SCALE = 6


def timestamp_fields(
    value: ExactTemporal, *, utc: bool = False
) -> tuple[int, int, int, int, int, int, dt.timedelta | None]:
    """Retain Gregorian fields without requiring Python to represent their year."""
    try:
        calendar = value.calendar_datetime(utc=utc)
    except OverflowError:
        pass
    else:
        return (
            calendar.year,
            calendar.month,
            calendar.day,
            calendar.hour,
            calendar.minute,
            calendar.second,
            calendar.utcoffset(),
        )
    from analytics_toolkit.sql.backends.calendar_ticks import (  # noqa: PLC0415 -- avoid backend registry initialization cycle.
        civil_from_days,
    )

    offset = None
    if value.timezone is not None:
        zone = (
            dt.timezone.utc
            if utc and not value.offset_naive
            else canonical_named_zone(value.timezone)
        )
        offset = zone.utcoffset(None)
        if offset is None:
            msg = (
                "Exact timestamp timezone cannot be resolved outside Python's calendar; "
                "project UTC in source SQL."
            )
            raise ValueError(msg)
    seconds = value.ticks // 10**value.scale
    if offset is not None:
        seconds += offset.days * _DAY_SECONDS + offset.seconds
    days, clock = divmod(seconds, _DAY_SECONDS)
    year, month, day = civil_from_days(days)
    hour, clock = divmod(clock, 3600)
    minute, second = divmod(clock, 60)
    return year, month, day, hour, minute, second, None if value.offset_naive else offset


def format_timestamp(value: ExactTemporal, *, utc: bool = False) -> str:
    """Expanded ISO text with every declared fractional tick and offset."""
    from analytics_toolkit.sql.backends.calendar_ticks import (  # noqa: PLC0415 -- avoid backend registry initialization cycle.
        iso_date,
    )

    year, month, day, hour, minute, second, offset = timestamp_fields(value, utc=utc)
    result = f"{iso_date(year, month, day)}T{hour:02d}:{minute:02d}:{second:02d}"
    if value.scale:
        result += f".{value.ticks % 10**value.scale:0{value.scale}d}"
    if offset is not None:
        micros = (offset.days * _DAY_SECONDS + offset.seconds) * 10**_MICROSECOND_SCALE
        micros += offset.microseconds
        sign = "+" if micros >= 0 else "-"
        seconds, fraction = divmod(abs(micros), 10**_MICROSECOND_SCALE)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        result += f"{sign}{hours:02d}:{minutes:02d}"
        if seconds or fraction:
            result += f":{seconds:02d}"
            if fraction:
                result += f".{fraction:06d}".rstrip("0")
    return result
