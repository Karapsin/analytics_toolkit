"""Validate native integer ticks against the supported timestamp domain."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile

_MIN_DATETIME64_SECONDS = -2_208_988_800
_END_DATETIME64_SECONDS = 10_413_792_000
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def validate_integer_timestamp_range(profile: ColumnProfile, native_name: str, scale: int) -> None:
    """Check DateTime seconds or declared-scale DateTime64 ticks without rescaling input."""
    if profile.minimum_integer is None:
        return
    if native_name == "datetime":
        lower, upper = 0, 2**32 - 1
    else:
        ticks_per_second = 10**scale
        lower = max(_INT64_MIN, _MIN_DATETIME64_SECONDS * ticks_per_second)
        upper = min(_INT64_MAX, _END_DATETIME64_SECONDS * ticks_per_second - 1)
    if not lower <= profile.minimum_integer <= cast("int", profile.maximum_integer) <= upper:
        msg = "Selected input exceeds authoritative native integer timestamp range."
        raise ValueError(msg)
