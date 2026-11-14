"""Timestamp map keys follow the actual Trino parameter and destination casts."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends.trino.parameters import normalize_value
from analytics_toolkit.sql.backends.trino.timestamp_map_keys import (
    contains_timestamp_type,
    timestamp_key_identity,
    timestamp_key_value_and_identity,
)
from analytics_toolkit.sql.backends.type_resolution import ResolvedColumn
from analytics_toolkit.sql.core.exact_values import ExactTemporal
from zoneinfo import ZoneInfo

from tests.sql._support.trino_parameters import render_parameter


def wire(value, native):
    return render_parameter(normalize_value(value, native))


@pytest.mark.parametrize("precision", [0, 1, 3, 6, 9, 12])
@pytest.mark.parametrize("suffix", ["", " WITH TIME ZONE", " WITHOUT TIME ZONE"])
def test_exact_scale_aliases_have_one_native_identity_and_parameter(precision, suffix):
    native = f"TIMESTAMP({precision}){suffix}"
    values = [ExactTemporal(1, 0, "UTC"), ExactTemporal(1000, 3, "UTC")]
    results = [timestamp_key_value_and_identity(value, native) for value in values]
    assert [result[0] for result in results] == [True, True]
    assert [result[2] for result in results] == [10**precision] * 2
    assert results[0][1] == results[1][1]
    assert wire(results[0][1], native) == wire(results[1][1], native)
    assert values == [ExactTemporal(1, 0, "UTC"), ExactTemporal(1000, 3, "UTC")]


def test_python_pandas_numpy_and_exact_negative_nanoseconds_share_actual_parameter():
    values = [
        pd.Timestamp("1969-12-31 23:59:59.999999999"),
        np.datetime64("1969-12-31T23:59:59.999999999", "ns"),
        ExactTemporal(-1, 9),
    ]
    native = "TIMESTAMP(9)"
    for value in values:
        handled, payload, identity = timestamp_key_value_and_identity(value, native)
        assert handled
        assert identity == -1
        assert payload == ExactTemporal(-1, 9, wall_time=True)
        assert wire(payload, native) == "'1969-12-31 23:59:59.999999999'"


def test_timestamp12_preserves_distinct_nanoseconds_without_float_or_scale_clamping():
    native = "TIMESTAMP(12) WITH TIME ZONE"
    values = [ExactTemporal(ticks, 9, "UTC") for ticks in (-1, 0, 1, 2)]
    results = [timestamp_key_value_and_identity(value, native) for value in values]
    assert [result[2] for result in results] == [-1000, 0, 1000, 2000]
    normalized = [normalize_value(result[1], native) for result in results]
    assert len(set(normalized)) == 4
    assert render_parameter(normalized[0]) == "'1969-12-31 23:59:59.999999999000 UTC'"
    assert render_parameter(normalized[2]) == "'1970-01-01 00:00:00.000000001000 UTC'"
    assert render_parameter(normalized[3]) == "'1970-01-01 00:00:00.000000002000 UTC'"


def test_named_zone_and_python_pandas_utc_aliases_follow_actual_utc_writer():
    native = "TIMESTAMP(6) WITH TIME ZONE"
    values = [
        dt.datetime(2024, 1, 1, 12, 34, 56, 789123, tzinfo=ZoneInfo("Asia/Kolkata")),
        dt.datetime(2024, 1, 1, 7, 4, 56, 789123, tzinfo=dt.timezone.utc),
        pd.Timestamp("2024-01-01T07:04:56.789123Z"),
    ]
    results = [timestamp_key_value_and_identity(value, native) for value in values]
    assert len({result[2] for result in results}) == 1
    assert {wire(result[1], native) for result in results} == {
        "TIMESTAMP '2024-01-01 07:04:56.789123 UTC'"
    }


@pytest.mark.parametrize("precision", [6, 12])
@pytest.mark.parametrize("suffix", ["", " WITH TIME ZONE"])
def test_dst_fold_keys_remain_distinct_in_python_payload_and_actual_sdk_map(precision, suffix):
    zone = ZoneInfo("America/New_York")
    values = [dt.datetime(2024, 11, 3, 1, 30, tzinfo=zone, fold=fold) for fold in (0, 1)]
    assert values[0] == values[1]
    native = f"TIMESTAMP({precision}){suffix}"
    results = [timestamp_key_value_and_identity(value, native) for value in values]
    assert results[0][2] != results[1][2]
    payload = {results[0][1]: "earlier", results[1][1]: "later"}
    assert len(payload) == 2
    normalized = normalize_value(payload, f"MAP({native}, VARCHAR)")
    assert len(normalized) == 2
    rendered = render_parameter(normalized)
    assert "2024-11-03 05:30:00" in rendered
    assert "2024-11-03 06:30:00" in rendered
    assert list(normalized.values()) == ["earlier", "later"]
    assert [value.fold for value in values] == [0, 1]


def test_offset_naive_exact_identity_uses_visible_calendar_not_underlying_epoch_ticks():
    native = "TIMESTAMP(12)"
    local = ExactTemporal(0, 0, "America/New_York", offset_naive=True)
    wall = dt.datetime.fromisoformat("1969-12-31T19:00:00")
    first = timestamp_key_value_and_identity(local, native)
    second = timestamp_key_value_and_identity(wall, native)
    epoch = timestamp_key_value_and_identity(ExactTemporal(0, 0), native)
    assert first[2] == second[2] == -18_000 * 10**12
    assert epoch[2] == 0
    assert wire(first[1], native) == wire(second[1], native) == "'1969-12-31 19:00:00.000000000000'"


@pytest.mark.parametrize(
    "value",
    [
        dt.datetime.fromisoformat("2024-01-01T12:30:00"),
        pd.Timestamp("2024-01-01T12:30:00"),
        np.datetime64("2024-01-01T12:30:00", "ns"),
        ExactTemporal(0, 0),
        ExactTemporal(0, 0, "America/New_York", offset_naive=True),
    ],
)
def test_unresolved_naive_zoned_timestamp_keys_do_not_invent_session_timezone(value):
    with pytest.raises(ValueError, match="Cannot certify naive"):
        timestamp_key_value_and_identity(value, "TIMESTAMP(12) WITH TIME ZONE")


@pytest.mark.parametrize("precision", [6, 9, 12])
@pytest.mark.parametrize("suffix", ["", " WITH TIME ZONE"])
@pytest.mark.parametrize(
    ("wall", "policy_zone", "expected"),
    [
        ("2024-01-15T12:30:00.123456", "Asia/Kolkata", "2024-01-15 07:00:00.123456"),
        ("1969-12-31T23:59:59.123456", "UTC", "1969-12-31 23:59:59.123456"),
        ("1969-12-31T23:59:59.123456", "Asia/Kolkata", "1969-12-31 18:29:59.123456"),
        ("2024-01-15T12:30:00.123456", "America/New_York", "2024-01-15 17:30:00.123456"),
    ],
)
def test_explicit_destination_policy_is_applied_once_and_canonical_keys_are_idempotent(
    precision, suffix, wall, policy_zone, expected
):
    value = dt.datetime.fromisoformat(wall)
    native = f"TIMESTAMP({precision}){suffix}"
    policy = {"destination_timezone": policy_zone}
    first = timestamp_key_value_and_identity(value, native, **policy)
    second = timestamp_key_value_and_identity(first[1], native, **policy)
    assert first == second
    assert expected in wire(first[1], native)
    assert value.tzinfo is None


@pytest.mark.parametrize("precision", [6, 9, 12])
@pytest.mark.parametrize("policy_zone", ["Asia/Kolkata", "UTC", "America/New_York"])
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (ExactTemporal(0, 0, "America/New_York", offset_naive=True), "1969-12-31 19:00:00"),
        (np.datetime64("1970-01-01T00:00:00", "ns"), "1970-01-01 00:00:00"),
    ],
)
def test_plain_native_calendar_survives_repeated_conversion_with_known_policy(
    precision, policy_zone, value, expected
):
    native = f"TIMESTAMP({precision})"
    policy = {"destination_timezone": policy_zone}
    first = timestamp_key_value_and_identity(value, native, **policy)
    converted_again = ResolvedColumn(native, target_backend="trino", **policy).convert(first[1])
    second = timestamp_key_value_and_identity(converted_again, native, **policy)
    converted_third = ResolvedColumn(native, target_backend="trino", **policy).convert(second[1])
    third = timestamp_key_value_and_identity(converted_third, native, **policy)
    assert first == second == third
    assert expected in wire(second[1], native)


@pytest.mark.parametrize("wall", ["2024-03-10T02:30:00", "2024-11-03T01:30:00"])
def test_unresolved_gap_and_fold_policy_is_rejected_before_a_key_can_be_certified(wall):
    value = dt.datetime.fromisoformat(wall)
    with pytest.raises(ValueError, match=r"Nonexistent|Ambiguous"):
        timestamp_key_value_and_identity(
            value, "TIMESTAMP(12) WITH TIME ZONE", destination_timezone="America/New_York"
        )
    assert value.tzinfo is None


def test_unresolved_custom_datetime_zone_cannot_fall_back_to_machine_timezone():
    class UnresolvedZone(dt.tzinfo):
        def utcoffset(self, _value):
            return None

        def tzname(self, _value):
            return "unresolved timezone"

        def dst(self, _value):
            return None

    zone = UnresolvedZone()
    value = dt.datetime(2024, 1, 1, tzinfo=zone)
    with pytest.raises(ValueError, match="resolved UTC offset"):
        timestamp_key_value_and_identity(value, "TIMESTAMP(12) WITH TIME ZONE")
    assert value.tzinfo is zone


@pytest.mark.parametrize("suffix", ["", " WITH TIME ZONE", " WITHOUT TIME ZONE"])
@pytest.mark.parametrize(
    ("microseconds", "milliseconds"),
    [(-1500, -1), (-501, -1), (-500, 0), (-499, 0), (499, 0), (500, 1), (1500, 2)],
)
def test_bare_timestamp_native_millisecond_rounding_matches_both_signed_epoch_casts(
    suffix, microseconds, milliseconds
):
    native = "TIMESTAMP" + suffix
    value = ExactTemporal(microseconds, 6, "UTC")
    result = timestamp_key_value_and_identity(value, native)
    assert result[2] == milliseconds
    assert timestamp_key_identity(result[1], native) == (True, milliseconds)
    assert normalize_value(value, native) == value.calendar_datetime(utc=True)


def test_explicit_timestamp_precision_keeps_current_negative_floor_truncation_contract():
    native = "TIMESTAMP(6) WITH TIME ZONE"
    values = [ExactTemporal(-1, scale, "UTC") for scale in (9, 6)]
    assert [timestamp_key_identity(value, native) for value in values] == [(True, -1)] * 2
    assert {
        wire(timestamp_key_value_and_identity(value, native)[1], native) for value in values
    } == {"TIMESTAMP '1969-12-31 23:59:59.999999 UTC'"}


@pytest.mark.parametrize(
    "native", ["TIMESTAMP(13)", "TIMESTAMP(+3)", "TIMESTAMP(-0)", "ARRAY(TIMESTAMP(13))"]
)
def test_registered_invalid_timestamp_declarations_do_not_gain_key_certification(native):
    with pytest.raises(ValueError, match="precision"):
        contains_timestamp_type(native)


@pytest.mark.parametrize(
    "native", ["TIMESTAMP(6)[]", "TUPLE(TIMESTAMP(6))", "TUPLE(created_at TIMESTAMP(6))"]
)
def test_timestamp_container_spelling_must_be_native_trino(native):
    with pytest.raises(ValueError, match="native ARRAY, MAP or ROW"):
        contains_timestamp_type(native)


def test_key_family_null_and_supported_source_precision_boundaries_are_preserved():
    assert timestamp_key_value_and_identity(1, "BIGINT") == (False, None, None)
    assert timestamp_key_identity(1, None) == (False, None)
    assert contains_timestamp_type("MAP(ROW(x TIMESTAMP(12)), VARCHAR)")
    assert not contains_timestamp_type("MAP(BIGINT, VARCHAR)")
    with pytest.raises(ValueError, match="cannot be NULL"):
        timestamp_key_value_and_identity(None, "TIMESTAMP(12)")
    with pytest.raises(TypeError, match="native temporal"):
        timestamp_key_value_and_identity("1970-01-01", "TIMESTAMP(12)")
    with pytest.raises(ValueError, match="supported source fractional precision"):
        timestamp_key_value_and_identity(
            ExactTemporal(1, 13, "UTC"), "TIMESTAMP(12) WITH TIME ZONE"
        )
