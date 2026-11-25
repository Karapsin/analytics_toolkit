"""Trino SPI timestamp coordinates, independent of Python calendar limits.

The pinned integration server is Trino 482. ShortTimestampType and LongTimestamp
store signed64 epoch microseconds; both zoned representations pack milliseconds
with a 12-bit zone key, including LongTimestampWithTimeZoneType.writeObject.
https://github.com/trinodb/trino/tree/482/core/trino-spi/src/main/java/io/trino/spi/type
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile

_NANOSECONDS_PER_DAY = 86400 * 10**9


class TrinoTimestampRangeError(ValueError):
    analytics_toolkit_sql_retry_safe = False


def timestamp_coordinates(
    profile: ColumnProfile, *, aware: bool, destination_timezone: str | None = None
) -> tuple[int | None, int | None]:
    """Use certified instants, retaining an interval for unresolved naive walls."""
    if (
        aware
        and destination_timezone is not None
        and profile.context_valid
        and profile.context_value_count == profile.temporal_value_count
    ):
        return profile.minimum_context_timestamp_ns, profile.maximum_context_timestamp_ns
    lower = [profile.minimum_instant_timestamp_ns]
    upper = [profile.maximum_instant_timestamp_ns]
    naive_low, naive_high = profile.minimum_naive_timestamp_ns, profile.maximum_naive_timestamp_ns
    if aware and destination_timezone is None:
        naive_low = None if naive_low is None else naive_low - _NANOSECONDS_PER_DAY
        naive_high = None if naive_high is None else naive_high + _NANOSECONDS_PER_DAY
    lower.append(naive_low)
    upper.append(naive_high)
    # Old/custom profiles can carry only the compatibility range fields.
    if all(value is None for value in lower):
        lower.append(profile.minimum_timestamp_ns)
        upper.append(profile.maximum_timestamp_ns)
    return (
        min((value for value in lower if value is not None), default=None),
        max((value for value in upper if value is not None), default=None),
    )


def timestamp_type(native_type: str) -> tuple[int, bool] | None:
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native grammar initialization cycle.
        is_timestamp_type,
        parse_native_type,
    )

    if not is_timestamp_type(native_type):
        return None
    native = parse_native_type(native_type).base
    precision = int(native.arguments[0]) if native.arguments else 3
    return precision, "with time zone" in native.name


def validate_timestamp_profile(
    profile: ColumnProfile, native_type: str, *, destination_timezone: str | None = None
) -> None:
    """Reject native coordinate overflow without narrowing the exact calendar."""
    spec = timestamp_type(native_type)
    if spec is None:
        return
    _precision, aware = spec
    lower, upper = timestamp_coordinates(
        profile, aware=aware, destination_timezone=destination_timezone
    )
    # p>6 naive timestamps retain a picos-of-micro fraction; p>3 zoned
    # timestamps retain a picos-of-millisecond fraction. Half-open slots cover
    # those fractions; the separate precision validator checks alignment.
    bits, nanos = (52, 10**6) if aware else (64, 1000)
    minimum, exclusive = -(2 ** (bits - 1)) * nanos, 2 ** (bits - 1) * nanos
    if lower is not None and (lower < minimum or upper is None or upper >= exclusive):
        message = "Selected input exceeds the native Trino timestamp coordinate range."
        if (
            aware
            and profile.minimum_naive_timestamp_ns is not None
            and destination_timezone is None
        ):
            message = (
                "Resolve destination timezone or supply aware values before validating "
                "the native Trino timestamp range."
            )
        raise TrinoTimestampRangeError(message)
