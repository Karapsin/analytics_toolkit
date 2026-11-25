"""Frozen destination timestamp interpretation, separate from SQL schema identity."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import pandas as pd
from sqlglot import exp, parse_one

from analytics_toolkit.sql.core.temporal_zones import (
    TIMEZONE_POLICY_VERSION,
    UnresolvedWallTimeError,
    canonical_named_zone,
    localize_wall,
    timezone_info,
)

__all__ = ["TIMEZONE_POLICY_VERSION", "canonical_named_zone", "localize_wall", "timezone_info"]
_MICROSECOND_SCALE = 6

if TYPE_CHECKING:
    from .value_profiles import ColumnProfile


def effective_timezone(target: str | None, destination_timezone: str | None = None) -> str | None:
    from .type_resolution import parse_native_type  # noqa: PLC0415 - native grammar cycle

    native = parse_native_type(target).base
    if (
        target
        and native.name not in {"datetime", "datetime64"}
        and not native.name.startswith("timestamp")
    ):
        return None
    index = 1 if native.name == "datetime64" else 0
    if native.name in {"datetime", "datetime64"} and len(native.arguments) > index:
        literal = parse_one(native.arguments[index], read="clickhouse")
        if not isinstance(literal, exp.Literal) or not literal.is_string:
            msg = "Timestamp timezone must be a static timezone name."
            raise ValueError(msg)
        return str(literal.this)
    return destination_timezone


def localize_value(value: Any, target: str, destination_timezone: str | None = None) -> Any:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 - exact value cycle
        ExactTemporal,
    )

    if isinstance(value, ExactTemporal):
        return _localize_exact_wall(value, effective_timezone(target, destination_timezone))
    is_date = isinstance(value, dt.date) and not isinstance(value, dt.datetime)
    if not isinstance(value, (dt.date, pd.Timestamp)):
        return value
    zone = effective_timezone(target, destination_timezone)
    if is_date:
        stamp = dt.datetime.combine(value, dt.time())
        return localize_wall(stamp, timezone_info(zone)) if zone is not None else stamp
    if value.tzinfo is not None or zone is None:
        return value
    if not isinstance(value, pd.Timestamp):
        return localize_wall(value, timezone_info(zone))
    selected = timezone_info(zone)
    localize_wall(value.to_pydatetime(warn=False), selected)
    return value.tz_localize(selected)


def _localize_exact_wall(value: Any, zone: str | None) -> Any:
    """Resolve only explicitly marked wall coordinates, retaining every raw tick."""
    if not value.wall_time or zone is None:
        return value
    from analytics_toolkit.sql.core.exact_values import ExactTemporal  # noqa: PLC0415

    selected_zone = timezone_info(zone)
    try:
        selected = localize_wall(value.calendar_datetime().replace(tzinfo=None), selected_zone)
    except OverflowError:
        offset = selected_zone.utcoffset(None)
        if offset is None:
            msg = (
                "Exact timestamp timezone cannot be resolved outside Python's calendar; "
                "project UTC in source SQL."
            )
            raise ValueError(msg) from None
    else:
        offset = selected.utcoffset() or dt.timedelta(0)
    offset_us = (offset.days * 86400 + offset.seconds) * 10**6 + offset.microseconds
    scale = value.scale
    offset_ticks = (
        offset_us * 10 ** (scale - _MICROSECOND_SCALE)
        if scale >= _MICROSECOND_SCALE
        else offset_us // 10 ** (_MICROSECOND_SCALE - scale)
    )
    return ExactTemporal(value.ticks_floor(scale) - offset_ticks, scale, zone)


def context_ticks(value: Any, zone: str | None) -> int:
    """Localize wall time without overflowing pandas' nanosecond container."""
    # Recursive exact temporal profile policy.
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415
        ExactTemporal,
    )

    if isinstance(value, ExactTemporal):
        return int(_localize_exact_wall(value, zone).ticks_floor(9))
    fraction = value.nanosecond if isinstance(value, pd.Timestamp) else 0
    stamp = value.to_pydatetime(warn=False) if isinstance(value, pd.Timestamp) else value
    if not isinstance(stamp, dt.datetime):
        stamp = dt.datetime.combine(stamp, dt.time())
    if stamp.tzinfo is None and zone is not None:
        stamp = localize_wall(stamp, timezone_info(zone))
    offset = stamp.utcoffset() or dt.timedelta(0)
    days = (stamp.date() - dt.date(1970, 1, 1)).days
    return int(
        (
            days * 86400
            + stamp.hour * 3600
            + stamp.minute * 60
            + stamp.second
            - offset.days * 86400
            - offset.seconds
        )
        * 10**9
        + (stamp.microsecond - offset.microseconds) * 1000
        + fraction
    )


def observe_context(profile: ColumnProfile, value: Any) -> None:
    # Recursive exact temporal profile policy.
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415
        ExactTemporal,
    )

    profile.temporal_value_count += 1
    zone = effective_timezone(profile.temporal_type, profile.destination_timezone)
    raw = isinstance(value, ExactTemporal)
    date_only = isinstance(value, dt.date) and not isinstance(value, dt.datetime)
    naive = value.wall_time if raw else date_only or value.tzinfo is None
    ticks = context_ticks(value, None)
    low, high = (
        ("minimum_naive_timestamp_ns", "maximum_naive_timestamp_ns")
        if naive
        else ("minimum_instant_timestamp_ns", "maximum_instant_timestamp_ns")
    )
    previous = getattr(profile, low)
    setattr(profile, low, ticks if previous is None else min(previous, ticks))
    previous = getattr(profile, high)
    setattr(profile, high, ticks if previous is None else max(previous, ticks))
    if naive and zone is None:
        return
    try:
        ticks = context_ticks(value, zone)
    except UnresolvedWallTimeError as exc:
        profile.wall_time_error = str(exc)
        profile.wall_time_error_zone = zone
        return
    profile.context_value_count += 1
    profile.minimum_context_timestamp_ns = (
        ticks
        if profile.minimum_context_timestamp_ns is None
        else min(profile.minimum_context_timestamp_ns, ticks)
    )
    profile.maximum_context_timestamp_ns = (
        ticks
        if profile.maximum_context_timestamp_ns is None
        else max(profile.maximum_context_timestamp_ns, ticks)
    )
