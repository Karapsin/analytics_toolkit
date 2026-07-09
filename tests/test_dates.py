from __future__ import annotations

from contextlib import nullcontext
from datetime import date, datetime

import pytest

import analytics_toolkit.dates as dates_module
import analytics_toolkit.dates.functions as date_functions
from analytics_toolkit.dates.dates import sanitize_date


def test_sanitize_date_converts_iso_string() -> None:
    assert sanitize_date("2026-05-18") == "20260518"


def test_sanitize_date_accepts_date_and_datetime() -> None:
    assert sanitize_date(date(2026, 5, 18)) == "20260518"
    assert sanitize_date(datetime(2026, 5, 18, 14, 30, 15)) == "20260518"


def test_sanitize_date_reuses_existing_validation() -> None:
    with pytest.raises(TypeError, match="Date value must be a string, date, or datetime"):
        sanitize_date(20260518)  # type: ignore[arg-type]


def test_sanitize_date_is_reexported() -> None:
    assert dates_module.sanitize_date("2026-05-18") == "20260518"
    assert date_functions.sanitize_date("2026-05-18") == "20260518"
    assert "sanitize_date" in dates_module.__all__


def test_quarter_helpers_are_supported_and_reexported() -> None:
    assert dates_module.first_day("2026-05-18", period="quarter") == "2026-04-01"
    assert dates_module.last_day("2026-05-18", period="quarter") == "2026-06-30"
    assert dates_module.add_quarters("2026-05-18", 2) == "2026-10-01"
    assert date_functions.add_quarters("2026-05-18", -1) == "2026-01-01"
    assert "add_quarters" in dates_module.__all__


def test_quarter_sequence_truncates_bounds_with_warnings() -> None:
    with pytest.warns(UserWarning) as warning_info:
        result = dates_module.gen_dates_list(
            "2026-02-15",
            "2026-10-20",
            interval="quarters",
        )

    assert result == ["2026-01-01", "2026-04-01", "2026-07-01", "2026-10-01"]
    messages = [str(warning.message) for warning in warning_info]
    assert any("start_dt was truncated" in message for message in messages)
    assert any("end_dt was truncated" in message for message in messages)


@pytest.mark.parametrize(
    ("interval", "expected_step_days"),
    [
        ("days", None),
        ("weeks", 7),
        ("months", None),
        ("quarters", None),
    ],
)
def test_generated_date_sequences_are_monotonic(interval: str, expected_step_days: int | None) -> None:
    warning_context = nullcontext() if interval == "days" else pytest.warns(UserWarning)
    with warning_context:
        values = dates_module.gen_dates_list(
            "2026-02-15",
            "2026-11-20",
            interval=interval,
            output_string=False,
        )
    assert values == sorted(values)
    if expected_step_days is not None and len(values) > 1:
        deltas = [(right - left).days for left, right in zip(values, values[1:])]
        assert set(deltas) == {expected_step_days}


@pytest.mark.parametrize(
    ("interval", "period"),
    [
        ("weeks", "week"),
        ("months", "month"),
        ("quarters", "quarter"),
    ],
)
def test_period_sequences_are_aligned_to_period_start(interval: str, period: str) -> None:
    with pytest.warns(UserWarning):
        values = dates_module.gen_dates_list(
            "2026-02-15",
            "2026-11-20",
            interval=interval,
        )
    assert all(value == dates_module.first_day(value, period=period) for value in values)


def test_is_greater_supports_strict_and_inclusive_comparison() -> None:
    assert dates_module.is_greater("2026-05-19", "2026-05-18")
    assert not dates_module.is_greater("2026-05-18", "2026-05-18")
    assert dates_module.is_greater("2026-05-18", "2026-05-18", inclusive=True)
    assert not dates_module.is_greater("2026-05-17", "2026-05-18", inclusive=True)


def test_is_less_supports_strict_and_inclusive_comparison() -> None:
    assert dates_module.is_less("2026-05-17", "2026-05-18")
    assert not dates_module.is_less("2026-05-18", "2026-05-18")
    assert dates_module.is_less("2026-05-18", "2026-05-18", inclusive=True)
    assert not dates_module.is_less("2026-05-19", "2026-05-18", inclusive=True)


def test_date_comparison_helpers_accept_date_and_datetime_inputs() -> None:
    assert dates_module.is_greater(date(2026, 5, 19), datetime(2026, 5, 18, 23, 59))
    assert dates_module.is_less(datetime(2026, 5, 17, 23, 59), date(2026, 5, 18))
    assert dates_module.is_between(
        datetime(2026, 5, 18, 12, 30),
        date(2026, 5, 18),
        date(2026, 5, 19),
    )


def test_is_between_supports_inclusive_and_exclusive_bounds() -> None:
    assert dates_module.is_between("2026-05-18", "2026-05-18", "2026-05-20")
    assert dates_module.is_between("2026-05-20", "2026-05-18", "2026-05-20")
    assert dates_module.is_between("2026-05-19", "2026-05-18", "2026-05-20", inclusive=False)
    assert not dates_module.is_between("2026-05-18", "2026-05-18", "2026-05-20", inclusive=False)
    assert not dates_module.is_between("2026-05-20", "2026-05-18", "2026-05-20", inclusive=False)


def test_is_between_rejects_reversed_bounds() -> None:
    with pytest.raises(ValueError, match="end_dt must be greater than or equal to start_dt"):
        dates_module.is_between("2026-05-18", "2026-05-20", "2026-05-18")


def test_date_comparison_helpers_are_reexported() -> None:
    assert date_functions.is_greater("2026-05-19", "2026-05-18")
    assert date_functions.is_less("2026-05-17", "2026-05-18")
    assert date_functions.is_between("2026-05-18", "2026-05-18", "2026-05-20")
    assert "is_greater" in dates_module.__all__
    assert "is_less" in dates_module.__all__
    assert "is_between" in dates_module.__all__
