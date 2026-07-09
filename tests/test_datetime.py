from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

import analytics_toolkit.datetime as datetime_module
import analytics_toolkit.datetime.functions as datetime_functions
from analytics_toolkit import datetime as dttm
from analytics_toolkit.datetime.datetime import format_datetime, sanitize_datetime


def test_format_datetime_accepts_supported_inputs_and_drops_microseconds() -> None:
    assert format_datetime("2026-01-01 12:13:15") == "2026-01-01 12:13:15"
    assert format_datetime("2026-01-01T12:13:15") == "2026-01-01 12:13:15"
    assert format_datetime("2026-01-01") == "2026-01-01 00:00:00"
    assert format_datetime(date(2026, 1, 1)) == "2026-01-01 00:00:00"
    assert (
        format_datetime(datetime(2026, 1, 1, 12, 13, 15, 123456))
        == "2026-01-01 12:13:15"
    )


def test_sanitize_datetime_returns_compact_timestamp() -> None:
    assert sanitize_datetime("2026-01-01 12:13:15") == "20260101121315"
    assert dttm.sanitize_datetime(date(2026, 1, 1)) == "20260101000000"


def test_datetime_validation_rejects_unsupported_and_timezone_aware_values() -> None:
    with pytest.raises(TypeError, match="Datetime value must be a string, date, or datetime"):
        format_datetime(20260101)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Datetime values must be timezone-naive"):
        format_datetime(datetime(2026, 1, 1, 12, 13, 15, tzinfo=timezone.utc))

    with pytest.raises(ValueError, match="Datetime values must be timezone-naive"):
        format_datetime("2026-01-01T12:13:15+00:00")


def test_add_helpers_preserve_time_components() -> None:
    value = "2026-01-01 12:13:15"

    assert dttm.add_seconds(value, 1) == "2026-01-01 12:13:16"
    assert dttm.add_minutes(value, 1) == "2026-01-01 12:14:15"
    assert dttm.add_hours(value, 1) == "2026-01-01 13:13:15"
    assert dttm.add_days(value, 1) == "2026-01-02 12:13:15"
    assert dttm.add_weeks(value, 1) == "2026-01-08 12:13:15"
    assert dttm.add_months("2026-01-31 12:13:15", 1) == "2026-02-28 12:13:15"
    assert dttm.add_quarters("2026-01-31 12:13:15", 1) == "2026-04-30 12:13:15"


def test_add_helpers_support_datetime_output() -> None:
    assert dttm.add_days("2026-01-01 12:13:15.987654", 1, output_string=False) == datetime(
        2026,
        1,
        2,
        12,
        13,
        15,
    )


def test_comparison_helpers_compare_full_timestamps() -> None:
    assert dttm.is_greater("2026-01-01 12:13:16", "2026-01-01 12:13:15")
    assert not dttm.is_greater("2026-01-01 12:13:15", "2026-01-01 12:13:15")
    assert dttm.is_greater(
        "2026-01-01 12:13:15",
        "2026-01-01 12:13:15",
        inclusive=True,
    )
    assert dttm.is_less("2026-01-01 12:13:14", "2026-01-01 12:13:15")
    assert dttm.is_less(
        "2026-01-01 12:13:15",
        "2026-01-01 12:13:15",
        inclusive=True,
    )


def test_is_between_supports_inclusive_and_exclusive_bounds() -> None:
    assert dttm.is_between(
        "2026-01-01 12:13:15",
        "2026-01-01 12:13:15",
        "2026-01-01 12:13:16",
    )
    assert dttm.is_between(
        "2026-01-01 12:13:15",
        "2026-01-01 12:13:14",
        "2026-01-01 12:13:16",
        inclusive=False,
    )
    assert not dttm.is_between(
        "2026-01-01 12:13:15",
        "2026-01-01 12:13:15",
        "2026-01-01 12:13:16",
        inclusive=False,
    )


def test_is_between_rejects_reversed_bounds() -> None:
    with pytest.raises(ValueError, match="end_dt must be greater than or equal to start_dt"):
        dttm.is_between(
            "2026-01-01 12:13:15",
            "2026-01-01 12:13:16",
            "2026-01-01 12:13:15",
        )


def test_difference_helpers_return_signed_whole_units() -> None:
    assert dttm.seconds_between("2026-01-01 12:13:15", "2026-01-01 12:13:45") == 30
    assert dttm.minutes_between("2026-01-01 12:13:15", "2026-01-01 12:43:14") == 29
    assert dttm.hours_between("2026-01-01 12:13:15", "2026-01-01 14:13:14") == 1
    assert dttm.days_between("2026-01-01 12:13:15", "2026-01-03 12:13:14") == 1
    assert dttm.minutes_between("2026-01-01 12:43:14", "2026-01-01 12:13:15") == -29
    assert dttm.hours_between("2026-01-01 14:13:14", "2026-01-01 12:13:15") == -1


def test_difference_helpers_support_inclusive_counts() -> None:
    assert dttm.seconds_between("2026-01-01 12:13:15", "2026-01-01 12:13:15", True) == 1
    assert dttm.minutes_between("2026-01-01 12:13:15", "2026-01-01 12:43:14", True) == 30
    assert dttm.minutes_between("2026-01-01 12:43:14", "2026-01-01 12:13:15", True) == -30
    assert dttm.hours_between("2026-01-01 12:13:15", "2026-01-01 12:43:14", True) == 1
    assert dttm.hours_between("2026-01-01 12:43:14", "2026-01-01 12:13:15", True) == -1


@pytest.mark.parametrize(
    ("period", "expected_start", "expected_end"),
    [
        ("minute", "2026-05-18 12:13:00", "2026-05-18 12:13:59"),
        ("hour", "2026-05-18 12:00:00", "2026-05-18 12:59:59"),
        ("day", "2026-05-18 00:00:00", "2026-05-18 23:59:59"),
        ("week", "2026-05-18 00:00:00", "2026-05-24 23:59:59"),
        ("month", "2026-05-01 00:00:00", "2026-05-31 23:59:59"),
        ("quarter", "2026-04-01 00:00:00", "2026-06-30 23:59:59"),
    ],
)
def test_datetime_bounds_supports_requested_periods(
    period: str,
    expected_start: str,
    expected_end: str,
) -> None:
    assert dttm.datetime_bounds("2026-05-18 12:13:15", period=period) == (
        expected_start,
        expected_end,
    )


def test_datetime_bounds_supports_datetime_output() -> None:
    assert dttm.datetime_bounds("2026-05-18 12:13:15", output_string=False) == (
        datetime(2026, 5, 18),
        datetime(2026, 5, 18, 23, 59, 59),
    )


def test_period_predicates_compare_exact_timestamp_boundaries() -> None:
    assert dttm.is_period_start("2026-05-18 12:13:00", period="minute")
    assert dttm.is_period_end("2026-05-18 12:13:59", period="minute")
    assert dttm.is_period_start("2026-05-18 12:00:00", period="hour")
    assert dttm.is_period_end("2026-05-18 12:59:59", period="hour")
    assert dttm.is_period_start("2026-05-18 00:00:00", period="day")
    assert dttm.is_period_end("2026-05-18 23:59:59", period="day")
    assert dttm.is_period_start("2026-04-01 00:00:00", period="quarter")
    assert dttm.is_period_end("2026-06-30 23:59:59", period="quarter")
    assert not dttm.is_period_start("2026-05-18 12:13:15", period="minute")
    assert not dttm.is_period_end("2026-05-18 12:13:58", period="minute")


def test_gen_datetimes_list_supports_all_intervals_and_aliases() -> None:
    assert dttm.gen_datetimes_list(
        "2026-01-01 12:13:15",
        "2026-01-01 12:13:17",
        interval="second",
    ) == [
        "2026-01-01 12:13:15",
        "2026-01-01 12:13:16",
        "2026-01-01 12:13:17",
    ]
    assert dttm.gen_datetimes_list(
        "2026-01-01 12:13:15",
        "2026-01-01 12:15:15",
        interval="minutes",
    ) == [
        "2026-01-01 12:13:15",
        "2026-01-01 12:14:15",
        "2026-01-01 12:15:15",
    ]
    assert dttm.gen_datetimes_list(
        "2026-01-01 12:13:15",
        "2026-01-01 14:13:15",
    ) == [
        "2026-01-01 12:13:15",
        "2026-01-01 13:13:15",
        "2026-01-01 14:13:15",
    ]
    assert dttm.gen_datetimes_list(
        "2026-01-01 12:13:15",
        "2026-01-03 12:13:15",
        interval="days",
    )[-1] == "2026-01-03 12:13:15"
    assert dttm.gen_datetimes_list(
        "2026-01-01 12:13:15",
        "2026-01-15 12:13:15",
        interval="weeks",
    )[-1] == "2026-01-15 12:13:15"
    assert dttm.gen_datetimes_list(
        "2026-01-31 12:13:15",
        "2026-03-31 12:13:15",
        interval="months",
    ) == [
        "2026-01-31 12:13:15",
        "2026-02-28 12:13:15",
        "2026-03-28 12:13:15",
    ]
    assert dttm.gen_datetimes_list(
        "2026-01-31 12:13:15",
        "2026-07-31 12:13:15",
        interval="quarters",
    ) == [
        "2026-01-31 12:13:15",
        "2026-04-30 12:13:15",
        "2026-07-30 12:13:15",
    ]


def test_gen_datetimes_list_supports_datetime_output_and_reversed_ranges() -> None:
    assert dttm.gen_datetimes_list(
        "2026-01-01 12:13:15",
        "2026-01-01 13:13:15",
        output_string=False,
    ) == [
        datetime(2026, 1, 1, 12, 13, 15),
        datetime(2026, 1, 1, 13, 13, 15),
    ]
    assert dttm.gen_datetimes_list("2026-01-02 12:13:15", "2026-01-01 12:13:15") == []


def test_invalid_periods_and_intervals_are_rejected() -> None:
    with pytest.raises(ValueError, match="period must be one of"):
        dttm.datetime_bounds("2026-01-01 12:13:15", period="year")

    with pytest.raises(ValueError, match="interval must be one of"):
        dttm.gen_datetimes_list("2026-01-01 12:13:15", "2026-01-01 13:13:15", interval="years")


def test_datetime_helpers_are_reexported() -> None:
    expected_names = {
        "sanitize_datetime",
        "format_datetime",
        "add_seconds",
        "add_minutes",
        "add_hours",
        "add_days",
        "add_weeks",
        "add_months",
        "add_quarters",
        "is_greater",
        "is_less",
        "is_between",
        "seconds_between",
        "minutes_between",
        "hours_between",
        "days_between",
        "datetime_bounds",
        "is_period_start",
        "is_period_end",
        "gen_datetimes_list",
    }

    assert set(datetime_module.__all__) == expected_names
    for name in expected_names:
        assert getattr(datetime_functions, name) is getattr(datetime_module, name)

    assert dttm.add_days("2026-01-01 12:13:15", 1) == "2026-01-02 12:13:15"
