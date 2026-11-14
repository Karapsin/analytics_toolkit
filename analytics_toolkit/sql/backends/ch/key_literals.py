"""ClickHouse predicates must use ClickHouse string and temporal literals."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from analytics_toolkit.sql.backends.key_literals import (
    render_transfer_literal as _standard_literal,
)


def _string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _native_exact(value: Any) -> Any:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 -- native scale bound.
        MAX_TEMPORAL_SCALE,
        ExactTemporal,
    )

    if value.scale <= MAX_TEMPORAL_SCALE:
        return value
    ticks, scale = value.ticks_floor(value.scale), value.scale
    while scale and ticks % 10 == 0:
        ticks //= 10
        scale -= 1
    if scale > MAX_TEMPORAL_SCALE:
        msg = "ClickHouse key predicates cannot preserve timestamp precision above nine."
        raise ValueError(msg)
    return ExactTemporal(ticks, scale, value.timezone, value.offset_naive, value.wall_time)


def render_transfer_literal(self: Any, value: Any) -> str:
    # The registry must finish loading before importing core.capabilities.
    from analytics_toolkit.sql.core.exact_values import ExactTemporal  # noqa: PLC0415

    if isinstance(value, ExactTemporal):
        value = _native_exact(value)
        # A retained source zone identifies the raw instant even when the
        # driver's public representation deliberately omitted the offset.
        aware = value.timezone is not None
        instant = ExactTemporal(value.ticks, value.scale, "UTC") if aware else value
        text = instant.isoformat(utc=aware).replace("T", " ", 1)
        if aware:
            text = text[:-6]
        zone = ", 'UTC'" if aware else ""
        return f"toDateTime64({_string(text)}, {value.scale}{zone})"
    if isinstance(value, datetime):
        aware = value.utcoffset() is not None
        if aware:
            value = value.astimezone(timezone.utc)
        # Timestamp.isoformat retains nanoseconds, unlike strftime or the
        # driver's Python datetime conversion. No float epoch is involved.
        text = value.isoformat(sep=" ")
        if aware:
            text = text[:-6]
        precision = len(text.rsplit(".", 1)[1]) if "." in text else 0
        zone = ", 'UTC'" if aware else ""
        return f"toDateTime64({_string(text)}, {precision}{zone})"
    if isinstance(value, date):
        return f"toDate32({_string(value.isoformat())})"
    if isinstance(value, str):
        return _string(value)
    return _standard_literal(self, value)
