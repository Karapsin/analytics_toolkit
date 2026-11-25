"""Integer-only ClickHouse temporal decoding shared by both wire protocols."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from numbers import Integral
from typing import Any

import pandas as pd

from .temporal_context import canonical_timezone, timezone_name

_MAX_SCALE = 9
_MICRO_SCALE = 6


def timestamp_from_ticks(
    ticks: int, scale: int, timezone: Any = None, *, offset_naive: bool = False
) -> Any:
    """Decode ticks without binary floating point or Python datetime narrowing."""
    if not 0 <= scale <= _MAX_SCALE:
        message = "ClickHouse DateTime64 scale must be between 0 and 9."
        raise ValueError(message)
    timezone = canonical_timezone(timezone)
    precision, unit = next(
        (p, u) for p, u in ((0, "s"), (3, "ms"), (6, "us"), (9, "ns")) if p >= scale
    )
    try:
        timestamp = pd.Timestamp(int(ticks) * 10 ** (precision - scale), unit=unit, tz="UTC")
    except (OverflowError, ValueError):
        from analytics_toolkit.sql.core.exact_values import ExactTemporal  # noqa: PLC0415

        if scale <= _MICRO_SCALE or ticks % 10 ** (scale - _MICRO_SCALE) == 0:
            micros = (
                ticks * 10 ** (_MICRO_SCALE - scale)
                if scale <= _MICRO_SCALE
                else ticks // 10 ** (scale - _MICRO_SCALE)
            )
            value = datetime(1970, 1, 1, tzinfo=dt_timezone.utc) + timedelta(microseconds=micros)
            if timezone is not None:
                value = value.astimezone(timezone)
            return value.replace(tzinfo=None) if timezone is None or offset_naive else value
        return ExactTemporal(
            int(ticks),
            scale,
            timezone_name(timezone),
            offset_naive=offset_naive,
        )
    if timezone is not None:
        timestamp = timestamp.tz_convert(timezone)
    if timezone is None or offset_naive:
        timestamp = timestamp.tz_localize(None)
    return timestamp


def timestamp_ticks(value: Any, scale: int, timezone: Any = None) -> int:
    """Encode using integer calendar arithmetic, including negative epochs."""
    from analytics_toolkit.sql.core.exact_values import ExactTemporal  # noqa: PLC0415

    if isinstance(value, ExactTemporal):
        if scale >= value.scale:
            return int(value.ticks * 10 ** (scale - value.scale))
        return int(value.ticks // 10 ** (value.scale - scale))
    if isinstance(value, Integral):
        return int(value)
    from analytics_toolkit.sql.backends.temporal_context import (  # noqa: PLC0415
        context_ticks,
        localize_wall,
    )

    zone = timezone_name(timezone)
    if timezone is not None and zone is None:
        fraction = value.nanosecond if isinstance(value, pd.Timestamp) else 0
        stamp = value.to_pydatetime(warn=False) if isinstance(value, pd.Timestamp) else value
        if isinstance(stamp, date) and not isinstance(stamp, datetime):
            stamp = datetime.combine(stamp, time())
        if stamp.tzinfo is None:
            stamp = localize_wall(stamp, timezone)
        ticks = context_ticks(stamp, None) + fraction
    else:
        ticks = context_ticks(value, zone)
    return int(ticks // 10 ** (9 - scale))


def datetime_from_seconds(
    ticks: int,
    timezone: Any = None,
    *,
    offset_naive: bool = False,
) -> datetime:
    timezone = canonical_timezone(timezone)
    value = datetime(1970, 1, 1, tzinfo=dt_timezone.utc) + timedelta(seconds=int(ticks))
    if timezone is not None:
        value = value.astimezone(timezone)
    return value.replace(tzinfo=None) if timezone is None or offset_naive else value
