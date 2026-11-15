"""Greenplum DATE certification preserves native session-owned datetime casts."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends.gp.date_values import (
    GreenplumDateValueError,
    contains_date_type,
    date_value,
    is_date_type,
    normalize_authoritative_date,
    validate_date_profile,
)
from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
from analytics_toolkit.sql.core.exact_values import ExactTemporal
from psycopg2.extensions import adapt


@pytest.mark.parametrize("native", ["DATE", "date", "pg_catalog.date", '"pg_catalog"."date"'])
def test_gp_builtin_date_identity_uses_the_existing_native_type_resolver(native):
    assert is_date_type(native)
    assert contains_date_type(native)
    assert normalize_authoritative_date("2024-02-29", native) == dt.date(2024, 2, 29)


@pytest.mark.parametrize("native", [None, "TEXT", "TIMESTAMP", "sandbox.date", "sandbox.date[]"])
def test_gp_non_date_and_user_defined_types_keep_their_native_assignment_value(native):
    value = object()

    assert not is_date_type(native)
    assert normalize_authoritative_date(value, native) is value
    if native is not None:
        assert not contains_date_type(native)
        assert validate_date_profile(ColumnProfile(), native) is False


@pytest.mark.parametrize("value", [None, pd.NA, pd.NaT])
def test_gp_date_nulls_are_native_sql_nulls(value):
    assert date_value(value) is None
    assert normalize_authoritative_date(value, "DATE") is None


@pytest.mark.parametrize(
    "value",
    [
        dt.datetime.fromisoformat("2024-01-01T00:30:00+14:00"),
        pd.Timestamp("2024-01-01T00:30:00.123456+14:00"),
    ],
    ids=["aware-datetime", "aware-timestamp"],
)
def test_gp_date_aware_parameters_retain_the_native_offset_and_actual_psycopg_wire(value):
    converted = date_value(value)

    assert converted is value
    assert normalize_authoritative_date(value, "DATE") is value
    assert converted.utcoffset() == dt.timedelta(hours=14)
    assert adapt(converted).getquoted() == f"'{value.isoformat()}'::timestamptz".encode()
    # This certifies the native parameter, not the day chosen by an unknown
    # database session's TimeZone when assigning that parameter to DATE.


def test_gp_date_native_calendar_objects_and_arrays_preserve_caller_ownership():
    stamp = dt.datetime.fromisoformat("2024-02-29T23:59:59.123456")
    day = dt.date(2024, 3, 1)
    values = [["2024-02-29", None], [stamp, day]]

    assert date_value(stamp) is stamp
    assert date_value(day) is day
    assert contains_date_type("DATE[][]")
    converted = normalize_authoritative_date(values, "DATE[][]")

    assert converted == [[dt.date(2024, 2, 29), None], [stamp, day]]
    assert converted is not values
    assert values == [["2024-02-29", None], [stamp, day]]
    assert values[1][0] is stamp


@pytest.mark.parametrize(
    "value", ["2024-02-30", "private-date-value", "01/02/2024", 1, True, Decimal("1.5")]
)
def test_gp_invalid_date_inputs_fail_deterministically_without_echoing_private_values(value):
    with pytest.raises(GreenplumDateValueError) as error:
        date_value(value)

    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert "private-date-value" not in str(error.value)
    assert "CAST the source expression AS DATE" in str(error.value)


def test_gp_date_profile_retains_valid_native_timestamp_parameters_without_a_timezone_guess():
    values = [
        None,
        dt.date(2024, 2, 29),
        dt.datetime.fromisoformat("2024-02-29T23:59:59"),
        dt.datetime.fromisoformat("2024-03-01T00:30:00+14:00"),
    ]
    profile = ColumnProfile()
    for value in values:
        profile.add(date_value(value))

    assert validate_date_profile(profile, "DATE")
    assert profile.families == {"date", "timestamp"}
    assert profile.null_count == 1
    assert profile.aware
    assert profile.naive

    invalid = ColumnProfile()
    invalid.add(1)
    with pytest.raises(GreenplumDateValueError):
        validate_date_profile(invalid, "DATE")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            ExactTemporal(1_123_456_789, 9, "+14:00"),
            dt.datetime.fromisoformat("1970-01-01T14:00:01.123456+14:00"),
        ),
        (
            ExactTemporal(1_123_456_789, 9, wall_time=True),
            dt.datetime.fromisoformat("1970-01-01T00:00:01.123456"),
        ),
        (
            ExactTemporal(-1, 9, wall_time=True),
            dt.datetime.fromisoformat("1969-12-31T23:59:59.999999"),
        ),
        (
            ExactTemporal(1_123_456, 6, "+14:00", offset_naive=True),
            dt.datetime.fromisoformat("1970-01-01T14:00:01.123456"),
        ),
    ],
    ids=["aware-nanoseconds", "wall-nanoseconds", "negative-wall", "declared-offset-naive"],
)
def test_gp_date_exact_temporal_values_bind_their_native_calendar_parameter(raw, expected):
    original = (raw.ticks, raw.scale, raw.timezone, raw.offset_naive, raw.wall_time)

    converted = date_value(raw)

    assert isinstance(converted, dt.datetime)
    assert converted == expected
    assert converted.utcoffset() == expected.utcoffset()
    assert converted.isoformat() == expected.isoformat()
    assert adapt(converted).getquoted() == adapt(expected).getquoted()
    assert date_value(converted) is converted
    assert (raw.ticks, raw.scale, raw.timezone, raw.offset_naive, raw.wall_time) == original


@pytest.mark.parametrize(
    "raw",
    [
        np.datetime64("1970-01-01T00:00:01.123456789", "ns"),
        np.datetime64("2024-02-29", "D"),
    ],
    ids=["nanoseconds", "date-unit"],
)
def test_gp_date_numpy_temporal_values_bind_native_naive_timestamp_parameters(raw):
    expected = pd.Timestamp(raw)

    converted = date_value(raw)

    assert isinstance(converted, pd.Timestamp)
    assert converted == expected
    assert converted.tzinfo is None
    assert converted.nanosecond == expected.nanosecond
    assert adapt(converted).getquoted() == f"'{expected.isoformat()}'::timestamp".encode()


@pytest.mark.parametrize(
    "raw",
    [
        ExactTemporal(10**30, 0),
        ExactTemporal(0, 13),
        ExactTemporal(0, 0, "private-date-zone"),
        np.datetime64("12000-01-01", "D"),
    ],
    ids=["calendar-overflow", "invalid-scale", "unknown-zone", "numpy-calendar-overflow"],
)
def test_gp_date_raw_temporal_failures_are_sanitized_before_native_assignment(raw):
    with pytest.raises(GreenplumDateValueError) as error:
        date_value(raw)

    assert "private-date-zone" not in str(error.value)
    assert error.value.analytics_toolkit_sql_retry_safe is False
