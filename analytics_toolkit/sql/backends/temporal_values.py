"""Exact date-to-midnight conversion and authoritative timestamp bounds."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .value_profiles import ColumnProfile


_MAX_SCALE = 9


def timestamp_scale(type_name: str | None) -> int:
    from .type_resolution import parse_native_type  # noqa: PLC0415 - native grammar cycle

    base = parse_native_type(type_name).base
    if base.name == "datetime":
        return 0
    if base.arguments and base.arguments[0].isdigit():
        return int(base.arguments[0])
    return 6


def timestamp_value(value: Any, target: str, destination_timezone: str | None = None) -> Any:
    from .temporal_context import localize_value  # noqa: PLC0415 - frozen destination context
    from .type_resolution import parse_native_type  # noqa: PLC0415 - native grammar cycle
    from .value_profiles import ColumnProfile  # noqa: PLC0415 - per-value preflight

    if parse_native_type(target).base.name in {"datetime", "datetime64"}:
        profile = ColumnProfile(destination_timezone=destination_timezone, temporal_type=target)
        profile.add(value)
        validate_timestamp_range(profile, target, destination_timezone)
    return localize_value(value, target, destination_timezone)


def _wall_ticks(ticks: int, zone: str) -> int:
    from .temporal_context import context_ticks  # noqa: PLC0415 - integer wall-clock arithmetic

    seconds, fraction = divmod(ticks, 10**9)
    epoch = dt.datetime(1970, 1, 1, tzinfo=None)  # noqa: DTZ001 - wall-clock reconstruction
    stamp = epoch + dt.timedelta(seconds=seconds, microseconds=fraction // 1000)
    return context_ticks(stamp, zone) + fraction % 1000


def validate_timestamp_range(
    profile: ColumnProfile, target: str, destination_timezone: str | None = None
) -> None:
    from .temporal_context import effective_timezone  # noqa: PLC0415 - frozen destination context
    from .type_resolution import parse_native_type  # noqa: PLC0415 - native grammar cycle

    scale = timestamp_scale(target)
    if not 0 <= scale <= _MAX_SCALE:
        msg = "Unsupported authoritative timestamp scale."
        raise ValueError(msg)
    zone = effective_timezone(target, destination_timezone)
    profile_zone = effective_timezone(profile.temporal_type, profile.destination_timezone)
    if profile.wall_time_error is not None and zone == profile.wall_time_error_zone:
        raise ValueError(profile.wall_time_error)
    if (
        profile.context_valid
        and zone == profile_zone
        and profile.context_value_count == profile.temporal_value_count
    ):
        lower, upper = profile.minimum_context_timestamp_ns, profile.maximum_context_timestamp_ns
    else:
        lower, upper = profile.minimum_instant_timestamp_ns, profile.maximum_instant_timestamp_ns
        for ticks in (profile.minimum_naive_timestamp_ns, profile.maximum_naive_timestamp_ns):
            if ticks is None:
                continue
            low = high = _wall_ticks(ticks, zone) if zone is not None else ticks
            if zone is None:
                low -= 86400 * 10**9
                high += 86400 * 10**9
            lower = low if lower is None else min(lower, low)
            upper = high if upper is None else max(upper, high)
    if lower is None:
        return
    native = parse_native_type(target).base.name
    minimum, maximum = (0, 2**32 - 1) if native == "datetime" else (-(2**63), 2**63 - 1)
    unit = 10 ** (_MAX_SCALE - scale)
    if (
        lower // unit < minimum
        or upper is None
        or upper // unit > maximum
        or (profile.minimum_date is not None and profile.minimum_date < dt.date(1900, 1, 1))
        or (profile.maximum_date is not None and profile.maximum_date > dt.date(2299, 12, 31))
    ):
        msg = "Selected input exceeds authoritative timestamp range."
        if zone is None and profile.minimum_naive_timestamp_ns is not None:
            msg = "Resolve destination timezone before validating authoritative timestamp range."
        raise ValueError(msg)
