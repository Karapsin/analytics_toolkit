"""Exact timestamp facts without a nanosecond-limited calendar container."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.core.exact_calendar import timestamp_fields
from analytics_toolkit.sql.core.exact_values import ExactTemporal

if TYPE_CHECKING:
    from .value_profiles import ColumnProfile

_DAY_SECONDS = 86400
_NANOSECOND_SCALE = 9
_MAX_PYTHON_YEAR = 9999


def observe_timestamp(profile: ColumnProfile, value: Any) -> None:
    """Collect range, awareness and fractional alignment over every input value."""
    profile.observe_temporal(value)
    if isinstance(value, ExactTemporal):
        profile.aware |= value.timezone is not None and not value.offset_naive
        profile.naive |= value.timezone is None or value.offset_naive
        fraction = value.ticks % 10**value.scale
        scale = value.scale
        ticks = value.ticks_floor(_NANOSECOND_SCALE)
        year, month, day, *_ = timestamp_fields(value, utc=True)
        if 1 <= year <= _MAX_PYTHON_YEAR:
            date = dt.date(year, month, day)
        else:
            profile.timestamp_calendar_outside_python = True
            date = None
    else:
        profile.aware |= value.tzinfo is not None
        profile.naive |= value.tzinfo is None
        fraction = value.microsecond * 1000 + getattr(value, "nanosecond", 0)
        scale = _NANOSECOND_SCALE
        stamp = value.astimezone(dt.timezone.utc) if value.tzinfo is not None else value
        days = (stamp.date() - dt.date(1970, 1, 1)).days
        ticks = (
            (days * _DAY_SECONDS + stamp.hour * 3600 + stamp.minute * 60 + stamp.second)
            * 10**_NANOSECOND_SCALE
            + stamp.microsecond * 1000
            + getattr(stamp, "nanosecond", 0)
        )
        date = stamp.date()
    while scale and fraction % 10 == 0:
        fraction //= 10
        scale -= 1
    profile.temporal_scale = max(profile.temporal_scale, scale)
    profile.minimum_timestamp_ns = (
        ticks if profile.minimum_timestamp_ns is None else min(profile.minimum_timestamp_ns, ticks)
    )
    profile.maximum_timestamp_ns = (
        ticks if profile.maximum_timestamp_ns is None else max(profile.maximum_timestamp_ns, ticks)
    )
    if date is not None:
        profile.add_date(date)
