"""Integer-only ClickHouse temporal decoding shared by both wire protocols."""

from __future__ import annotations

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from numbers import Integral
from typing import Any

import pandas as pd
from pytz import timezone as named_timezone

_MAX_SCALE = 9
_MICRO_SCALE = 6


def timestamp_from_ticks(
    ticks: int, scale: int, timezone: Any = None, *, offset_naive: bool = False
) -> Any:
    """Decode ticks without binary floating point or Python datetime narrowing."""
    if not 0 <= scale <= _MAX_SCALE:
        message = "ClickHouse DateTime64 scale must be between 0 and 9."
        raise ValueError(message)
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
                zone = named_timezone(timezone) if isinstance(timezone, str) else timezone
                value = value.astimezone(zone)
            return value.replace(tzinfo=None) if timezone is None or offset_naive else value
        return ExactTemporal(
            int(ticks),
            scale,
            str(timezone) if timezone is not None else None,
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
    timestamp = pd.Timestamp(value)
    if timestamp.tz is None and timezone is not None:
        timestamp = timestamp.tz_localize(timezone)
    if timestamp.tz is not None:
        timestamp = timestamp.tz_convert("UTC").tz_localize(None)
    delta = timestamp.to_pydatetime(warn=False).replace(tzinfo=dt_timezone.utc) - datetime(
        1970, 1, 1, tzinfo=dt_timezone.utc
    )
    nanos = ((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1000
    return int((nanos + timestamp.nanosecond) // 10 ** (9 - scale))
