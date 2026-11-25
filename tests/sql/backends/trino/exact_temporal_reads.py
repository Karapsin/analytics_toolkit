"""Raw Trino calendar strings retain their complete declared precision."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.trino.exact_temporal_reads import decode_time, decode_timestamp
from analytics_toolkit.sql.core.exact_values import ExactClock, ExactTemporal

FRACTION = "123456789123"


def wire(whole, precision, zone=None):
    fraction = "." + FRACTION[:precision] if precision else ""
    return whole + fraction + (" " + zone if zone is not None else "")


def clock_wire(whole, precision, zone=None):
    fraction = "." + FRACTION[:precision] if precision else ""
    return whole + fraction + (zone or "")


@pytest.mark.parametrize("precision", range(13))
def test_naive_negative_epoch_retains_every_fractional_digit(precision):
    value = decode_timestamp(wire("1969-12-31 23:59:59", precision), precision, with_timezone=False)
    digits = FRACTION[:precision]
    expected_ticks = -(10**precision) + int(digits or "0")
    if precision <= 6:
        assert type(value) is dt.datetime
        assert (value.year, value.month, value.day) == (1969, 12, 31)
        assert (value.hour, value.minute, value.second) == (23, 59, 59)
        assert value.microsecond == int(digits.ljust(6, "0") or "0")
        assert value.tzinfo is None
    elif precision <= 9:
        assert isinstance(value, pd.Timestamp)
        assert value.value == expected_ticks * 10 ** (9 - precision)
        assert value.tzinfo is None
    else:
        assert value == ExactTemporal(expected_ticks, precision, wall_time=True)


@pytest.mark.parametrize("precision", range(13))
def test_fixed_zone_preserves_wall_calendar_offset_and_exact_instant(precision):
    value = decode_timestamp(
        wire("1970-01-01 05:30:00", precision, "+05:30"), precision, with_timezone=True
    )
    expected_ticks = int(FRACTION[:precision] or "0")
    if precision <= 6:
        assert type(value) is dt.datetime
        assert (value.hour, value.minute, value.second) == (5, 30, 0)
        assert value.utcoffset() == dt.timedelta(hours=5, minutes=30)
        assert value.microsecond == int(FRACTION[:precision].ljust(6, "0") or "0")
    elif precision <= 9:
        assert isinstance(value, pd.Timestamp)
        assert value.value == expected_ticks * 10 ** (9 - precision)
        assert value.utcoffset() == dt.timedelta(hours=5, minutes=30)
    else:
        assert value == ExactTemporal(expected_ticks, precision, "+05:30")


@pytest.mark.parametrize("precision", range(13))
@pytest.mark.parametrize("with_timezone", [False, True])
def test_null_is_sql_null_at_every_native_precision(precision, with_timezone):
    assert decode_timestamp(None, precision, with_timezone=with_timezone) is None


@pytest.mark.parametrize("precision", [0, 6, 9, 12])
def test_named_zone_retains_original_zone_when_local_wall_is_unambiguous(precision):
    value = decode_timestamp(
        wire("2024-07-01 12:30:45", precision, "America/New_York"),
        precision,
        with_timezone=True,
    )
    if isinstance(value, ExactTemporal):
        assert value.timezone == "America/New_York"
        calendar = value.calendar_datetime()
    else:
        calendar = value
        assert calendar.tzinfo.key == "America/New_York"
    assert (calendar.hour, calendar.minute, calendar.second) == (12, 30, 45)
    assert calendar.utcoffset() == -dt.timedelta(hours=4)


@pytest.mark.parametrize("precision", [0, 6, 9, 12])
@pytest.mark.parametrize("whole", ["2024-11-03 01:30:00", "2024-03-10 02:30:00"])
def test_named_zone_fold_or_gap_rejects_without_inventing_an_instant(precision, whole):
    raw = wire(whole, precision, "America/New_York")

    with pytest.raises(ValueError, match="project UTC in source SQL") as error:
        decode_timestamp(raw, precision, with_timezone=True)

    assert raw not in str(error.value)
    assert "2024" not in str(error.value)


def test_numeric_offsets_disambiguate_the_two_fall_back_instants():
    first = decode_timestamp("2024-11-03 01:30:00.000000001 -04:00", 9, with_timezone=True)
    second = decode_timestamp("2024-11-03 01:30:00.000000001 -05:00", 9, with_timezone=True)

    assert isinstance(first, pd.Timestamp)
    assert isinstance(second, pd.Timestamp)
    assert second.value - first.value == 3600 * 10**9
    assert first.nanosecond == 1
    assert second.nanosecond == 1


@pytest.mark.parametrize("zone", [None, "UTC", "-03:30"])
def test_picosecond_reads_keep_original_scale_even_when_microsecond_aligned(zone):
    value = decode_timestamp(
        "1970-01-01 00:00:00.123456000000" + (" " + zone if zone else ""),
        12,
        with_timezone=zone is not None,
    )
    expected = 123456000000 + (12600 * 10**12 if zone == "-03:30" else 0)

    assert value == ExactTemporal(expected, 12, zone, wall_time=zone is None)


def test_short_fraction_is_padded_to_metadata_scale_without_float():
    value = decode_timestamp("1969-12-31 23:59:59.123456789 UTC", 12, with_timezone=True)

    assert value == ExactTemporal(-876543211000, 12, "UTC")


def test_extra_zero_digits_are_exact_when_the_metadata_precision_is_lower():
    value = decode_timestamp("2024-01-01 01:02:03.120000", 2, with_timezone=False)

    assert type(value) is dt.datetime
    assert value.microsecond == 120000


@pytest.mark.parametrize("year", [1, 3000, 9999])
def test_python_supported_calendar_outside_nanosecond_range_uses_exact_fallback(year):
    raw = f"{year:04d}-01-01 00:00:00.000000001"
    value = decode_timestamp(raw, 9, with_timezone=False)
    seconds = (dt.date(year, 1, 1) - dt.date(1970, 1, 1)).days * 86400

    assert value == ExactTemporal(seconds * 10**9 + 1, 9, wall_time=True)


@pytest.mark.parametrize("year", [1, 9999])
def test_native_microseconds_keep_sdk_python_calendar_range(year):
    value = decode_timestamp(f"{year:04d}-01-01 00:00:00.000001", 6, with_timezone=False)

    assert type(value) is dt.datetime
    assert value.year == year
    assert value.microsecond == 1


@pytest.mark.parametrize("raw", ["0000-02-30 00:00:00", "+10000-13-01 00:00:00"])
def test_unsupported_calendar_fails_with_an_actionable_sanitized_error(raw):
    with pytest.raises(ValueError, match="invalid Gregorian calendar fields") as error:
        decode_timestamp(raw, 0, with_timezone=False)

    assert raw not in str(error.value)


def test_unrepresentable_utc_calendar_keeps_exact_high_precision_output():
    raw = "0001-01-01 00:00:00.000000001 +14:00"

    value = decode_timestamp(raw, 9, with_timezone=True)
    assert isinstance(value, ExactTemporal)
    assert value.scale == 9
    assert value.isoformat(utc=True) == "0000-12-31T10:00:00.000000001+00:00"


@pytest.mark.parametrize("precision", [-1, 13])
def test_precision_outside_native_domain_rejects_even_null(precision):
    with pytest.raises(ValueError, match="between 0 and 12"):
        decode_timestamp(None, precision, with_timezone=False)


@pytest.mark.parametrize("precision", [True, 3.0, "3"])
def test_non_integer_metadata_precision_rejects(precision):
    with pytest.raises(TypeError, match="integer"):
        decode_timestamp(None, precision, with_timezone=False)


@pytest.mark.parametrize(
    ("raw", "precision", "with_timezone", "message"),
    [
        ("2024-01-01 00:00:00.1234", 3, False, "declared precision"),
        ("2024-01-01 00:00:00.1234567891234", 12, False, "representation"),
        ("2024-01-01T00:00:00", 0, False, "representation"),
        ("2024-01-01 00:00:00", 0, True, "timezone family"),
        ("2024-01-01 00:00:00 UTC", 0, False, "timezone family"),
        ("2024-01-01 00:00:00 +14:01", 0, True, "offset"),
        ("2024-01-01 00:00:00 +05:60", 0, True, "offset"),
        ("2024-01-01 00:00:00 +01:00:00", 0, True, "offset"),
        ("2024-01-01 00:00:00 Not/A_Zone", 0, True, "project UTC"),
        ("2024-01-01 24:00:00", 0, False, "calendar"),
    ],
)
def test_inconsistent_wire_values_fail_before_any_loss(raw, precision, with_timezone, message):
    with pytest.raises(ValueError, match=message) as error:
        decode_timestamp(raw, precision, with_timezone=with_timezone)

    assert raw not in str(error.value)


def test_non_calendar_protocol_value_rejects_without_stringifying_it():
    with pytest.raises(TypeError, match="calendar string"):
        decode_timestamp(123, 6, with_timezone=False)


@pytest.mark.parametrize("precision", range(13))
@pytest.mark.parametrize(("zone", "offset"), [(None, None), ("+00:00", 0), ("-03:30", -210)])
def test_date_free_clock_retains_all_native_fractional_ticks(precision, zone, offset):
    digits = FRACTION[:precision]
    value = decode_time(
        clock_wire("23:59:59", precision, zone), precision, with_timezone=zone is not None
    )
    if precision <= 6:
        assert type(value) is dt.time
        assert (value.hour, value.minute, value.second) == (23, 59, 59)
        assert value.microsecond == int(digits.ljust(6, "0") or "0")
        assert value.utcoffset() == (dt.timedelta(minutes=offset) if offset is not None else None)
    else:
        assert value == ExactClock(86399 * 10**precision + int(digits), precision, offset)


@pytest.mark.parametrize("precision", range(7, 13))
@pytest.mark.parametrize("zone", [None, "+00:00", "+14:00", "-14:00"])
def test_microsecond_aligned_high_precision_clock_keeps_native_time(precision, zone):
    fraction = "123456000000"[:precision]
    raw = "12:30:45." + fraction + (zone or "")
    value = decode_time(raw, precision, with_timezone=zone is not None)

    assert type(value) is dt.time
    assert (value.hour, value.minute, value.second, value.microsecond) == (12, 30, 45, 123456)
    if zone is None:
        assert value.tzinfo is None
    elif zone == "+00:00":
        assert value.utcoffset() == dt.timedelta(0)
    else:
        assert abs(value.utcoffset()) == dt.timedelta(hours=14)


@pytest.mark.parametrize(
    ("raw", "ticks", "offset"),
    [
        ("00:00:00.000000000001", 1, None),
        ("23:59:59.999999999999", 86400 * 10**12 - 1, None),
        ("00:00:00.000000000001-14:00", 1, -840),
        ("23:59:59.999999999999+14:00", 86400 * 10**12 - 1, 840),
    ],
)
def test_picosecond_clock_bounds_keep_wall_ticks_without_an_invented_date(raw, ticks, offset):
    value = decode_time(raw, 12, with_timezone=offset is not None)

    assert value == ExactClock(ticks, 12, offset)


@pytest.mark.parametrize("precision", range(13))
def test_null_clock_remains_sql_null(precision):
    assert decode_time(None, precision, with_timezone=False) is None
    assert decode_time(None, precision, with_timezone=True) is None


@pytest.mark.parametrize("precision", [0, 6, 9, 12])
def test_clock_offset_space_tolerance_preserves_the_native_compact_value(precision):
    compact = clock_wire("12:30:45", precision, "+03:00")
    spaced = wire("12:30:45", precision, "+03:00")

    assert decode_time(spaced, precision, with_timezone=True) == decode_time(
        compact, precision, with_timezone=True
    )


@pytest.mark.parametrize(
    ("raw", "precision", "with_timezone", "message"),
    [
        ("00:00:00.1234", 3, False, "declared precision"),
        ("00:00:00.1234567891234", 12, False, "representation"),
        ("24:00:00.000000001", 9, False, "clock fields"),
        ("00:00:60.000000001", 9, False, "clock fields"),
        ("00:00:00.000000001", 9, True, "timezone family"),
        ("00:00:00.000000001+00:00", 9, False, "timezone family"),
        ("00:00:00.000000001+14:01", 9, True, "offset"),
        ("00:00:00.000000001 UTC", 9, True, "offset"),
        ("00:00:00.000000001 America/New_York", 9, True, "offset"),
    ],
)
def test_invalid_clock_wire_rejects_before_driver_rounding(raw, precision, with_timezone, message):
    with pytest.raises(ValueError, match=message) as error:
        decode_time(raw, precision, with_timezone=with_timezone)

    assert raw not in str(error.value)


def test_non_string_clock_protocol_value_rejects_without_stringifying_it():
    with pytest.raises(TypeError, match="clock string"):
        decode_time(123, 6, with_timezone=False)
