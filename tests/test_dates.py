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
