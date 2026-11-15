"""Trino DATE declarations and value views retain deliberate calendar casts."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends.trino.date_values import (
    TrinoDateValueError,
    contains_date_type,
    date_value,
    is_date_type,
    normalize_authoritative_date,
    validate_date_type_profile,
)
from analytics_toolkit.sql.backends.type_resolution import ColumnProfile, validate_profile
from analytics_toolkit.sql.core.exact_values import ExactTemporal


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-02-29", dt.date(2024, 2, 29)),
        (dt.date(2024, 2, 29), dt.date(2024, 2, 29)),
        (dt.datetime.fromisoformat("2024-02-29T17:30:00"), dt.date(2024, 2, 29)),
        (pd.Timestamp("2024-02-29T12:13:14.123456789"), dt.date(2024, 2, 29)),
        (np.datetime64("2024-02-29T12:13"), dt.date(2024, 2, 29)),
        (
            dt.datetime(2024, 3, 1, 0, 30, tzinfo=dt.timezone(dt.timedelta(hours=14))),
            dt.date(2024, 3, 1),
        ),
        (ExactTemporal(0, 0, "UTC"), dt.date(1970, 1, 1)),
    ],
)
def test_trino_date_values_retain_the_native_local_calendar(value, expected):
    converted = date_value(value)

    assert type(converted) is dt.date
    assert converted == expected
    assert normalize_authoritative_date(value, "DATE") == expected


@pytest.mark.parametrize("value", [None, pd.NA, pd.NaT, np.datetime64("NaT")])
def test_trino_date_null_assignment_view_remains_null(value):
    assert normalize_authoritative_date(value, "DATE") is None


@pytest.mark.parametrize(
    "value", [86400, True, 1.5, b"2024-02-29", "private-date-token", "2024-02-30"]
)
def test_trino_date_invalid_values_are_deterministic_and_private(value):
    with pytest.raises(TrinoDateValueError, match="CAST the source expression AS DATE") as error:
        date_value(value)

    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert str(value) not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("native", ["DATE", "date", "  DaTe  "])
def test_trino_date_type_check_keeps_family_validation_authoritative(native):
    assert is_date_type(native) is True
    assert validate_date_type_profile(native) is False
    raw_profile = ColumnProfile()
    raw_profile.add("2024-02-29")

    with pytest.raises(ValueError, match="authoritative date schema"):
        validate_profile(raw_profile, native, "trino")

    converted_profile = ColumnProfile()
    converted_profile.add(normalize_authoritative_date("2024-02-29", native))
    validate_profile(converted_profile, native, "trino")


@pytest.mark.parametrize("native", ["DATE(3)", "DATE()", "Nullable(DATE)", "LowCardinality(DATE)"])
def test_trino_date_invalid_declarations_reject_even_empty_profiles(native):
    with pytest.raises(TrinoDateValueError, match="unparameterized DATE"):
        is_date_type(native)

    with pytest.raises(TrinoDateValueError, match="unparameterized DATE"):
        validate_profile(ColumnProfile(), native, "trino")


@pytest.mark.parametrize(
    "native", ["ARRAY(DATE(3))", "MAP(VARCHAR, DATE(3))", "ROW(good DATE, bad DATE(3))"]
)
def test_trino_nested_date_declarations_reject_before_an_empty_container_is_written(native):
    with pytest.raises(TrinoDateValueError, match="unparameterized DATE"):
        validate_profile(ColumnProfile(), native, "trino")


def test_trino_date_views_leave_other_authoritative_families_unchanged():
    value = ["2024-02-29", None]

    assert is_date_type("VARCHAR") is False
    assert contains_date_type("ARRAY(VARCHAR)") is False
    assert normalize_authoritative_date(value, None) is value
    assert normalize_authoritative_date(value, "ARRAY(VARCHAR)") is value


def test_trino_nested_date_views_preserve_input_and_convert_every_date_leaf():
    values = [(pd.Timestamp("2024-02-29T12:30"), "leap"), (None, "missing")]
    before = deepcopy(values)

    converted = normalize_authoritative_date(values, "ARRAY(ROW(day DATE, label VARCHAR))")

    assert converted == [(dt.date(2024, 2, 29), "leap"), (None, "missing")]
    assert converted is not values
    assert values == before


def test_trino_nested_date_map_keys_reject_calendar_collisions_before_dict_conversion():
    values = {"2024-02-29": "first", pd.Timestamp("2024-02-29T12:30"): "second"}
    before = list(values.items())

    with pytest.raises(ValueError, match="map keys are not unique"):
        normalize_authoritative_date(values, "MAP(DATE, VARCHAR)")

    assert list(values.items()) == before
    assert len(values) == 2
