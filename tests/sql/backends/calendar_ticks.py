"""Gregorian tick arithmetic has no Python-calendar or nanosecond-range bound."""

from __future__ import annotations

import datetime as dt

import pytest
from analytics_toolkit.sql.backends.calendar_ticks import civil_from_days, days_from_civil, iso_date


@pytest.mark.parametrize(
    "fields",
    [
        (1, 1, 1),
        (1600, 2, 29),
        (1900, 3, 1),
        (1969, 12, 31),
        (1970, 1, 1),
        (2000, 2, 29),
        (9999, 12, 31),
    ],
)
def test_gregorian_days_match_independent_python_calendar(fields):
    expected = (dt.date(*fields) - dt.date(1970, 1, 1)).days
    assert days_from_civil(*fields) == expected
    assert civil_from_days(expected) == fields


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ((0, 1, 1), "0000-01-01"),
        ((0, 2, 29), "0000-02-29"),
        ((-1, 12, 31), "-0001-12-31"),
        ((-400, 2, 29), "-0400-02-29"),
        ((10000, 1, 1), "+10000-01-01"),
        ((1000000, 3, 1), "+1000000-03-01"),
    ],
)
def test_expanded_gregorian_years_remain_exact(fields, expected):
    assert civil_from_days(days_from_civil(*fields)) == fields
    assert iso_date(*fields) == expected
    following = civil_from_days(days_from_civil(*fields) + 146097)
    assert following == (fields[0] + 400, fields[1], fields[2])


@pytest.mark.parametrize(
    "fields", [(1900, 2, 29), (10000, 2, 30), (-100, 2, 29), (0, 0, 1), (0, 13, 1)]
)
def test_expanded_gregorian_validation_rejects_invalid_fields(fields):
    with pytest.raises(ValueError, match="Gregorian"):
        days_from_civil(*fields)
