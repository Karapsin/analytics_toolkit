"""Greenplum clock and interval precision preserves native component semantics."""

from __future__ import annotations

import datetime as dt
import json
from enum import Enum

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend
from analytics_toolkit.sql.backends.gp.temporal_values import (
    IntervalTransport,
    TimeTransport,
    convert_clock_interval_value,
    freeze_clock_interval_value,
    infer_clock_interval_type,
    is_gp_clock_interval_type,
    temporal_family,
    validate_clock_interval_profile,
)
from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
from analytics_toolkit.sql.core.exact_values import decode_value, typed_text
from analytics_toolkit.sql.core.replay_spool import ReplaySpool
from psycopg2.extensions import adapt
from zoneinfo import ZoneInfo

from tests.sql._support.gp_arrays import AdaptedConnection


def _profile(*values):
    profile = ColumnProfile()
    for value in values:
        profile.add(value)
    return profile


@pytest.mark.parametrize(
    ("native", "value", "expected"),
    [
        ("INTERVAL(3) DAY", dt.timedelta(days=1, seconds=2), "1 days +0.000000 seconds"),
        (
            "INTERVAL(2) HOUR TO MINUTE",
            dt.timedelta(days=-1, hours=2, minutes=3, seconds=4),
            "-1 days +7380.000000 seconds",
        ),
        ("INTERVAL(1) YEAR TO MONTH", dt.timedelta(days=1), "0 days +0.000000 seconds"),
        (
            "INTERVAL(0) DAY TO SECOND",
            dt.timedelta(days=1, seconds=2, microseconds=500000),
            "1 days +3.000000 seconds",
        ),
    ],
)
def test_temporal_values_prefix_interval_precision_keeps_native_field_semantics(
    native, value, expected
):
    assert is_gp_clock_interval_type(native)
    with pytest.raises(ValueError, match="precision or fields"):
        validate_clock_interval_profile(
            _profile(value), native, allow_timestamp_precision_loss=False
        )
    assert validate_clock_interval_profile(_profile(value), native)
    converted = convert_clock_interval_value(value, native)
    assert converted == expected
    assert validate_clock_interval_profile(
        _profile(converted), native, allow_timestamp_precision_loss=False
    )
    connection = AdaptedConnection()
    get_backend("gp").insert_rows_batch(
        connection,
        "sandbox.intervals",
        ["value"],
        [[converted]],
        target_column_types={"value": native},
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    assert adapt(expected).getquoted() in connection.target.statements[0]
    assert connection.committed


def test_temporal_values_prefix_precision_preserves_exact_observed_days_and_precision_bounds():
    profile = _profile(dt.timedelta(days=2))
    assert validate_clock_interval_profile(
        profile, "INTERVAL(3) DAY", allow_timestamp_precision_loss=False
    )
    with pytest.raises(ValueError, match=r"precision.*between 0 and 6"):
        validate_clock_interval_profile(profile, "INTERVAL(7) DAY")


@pytest.mark.parametrize(
    "native", ["pg_catalog.interval(3)", '"interval"(3)', '"pg_catalog"."interval"(3)']
)
def test_temporal_values_catalog_interval_masks_are_not_misread_as_sql_precision(native):
    assert not is_gp_clock_interval_type(native)
    assert not validate_clock_interval_profile(_profile(dt.timedelta(seconds=1)), native)
    with pytest.raises(ValueError, match="supported Greenplum"):
        convert_clock_interval_value(dt.timedelta(seconds=1), native)


@pytest.mark.parametrize(
    ("precision", "microseconds", "expected"),
    [
        (0, 500000, "12:34:57.000000"),
        (1, 150000, "12:34:56.200000"),
        (2, 125000, "12:34:56.130000"),
        (3, 123500, "12:34:56.124000"),
        (4, 123450, "12:34:56.123500"),
        (5, 123455, "12:34:56.123460"),
        (6, 123456, "12:34:56.123456"),
    ],
)
def test_temporal_values_clock_rounds_each_native_precision(precision, microseconds, expected):
    value = dt.time(12, 34, 56, microseconds)
    result = convert_clock_interval_value(value, f"TIME({precision})")
    assert isinstance(result, TimeTransport)
    assert result == expected
    assert temporal_family(result) == "time"
    assert adapt(result).getquoted() == f"'{expected}'".encode()


@pytest.mark.parametrize(
    ("precision", "microseconds", "seconds"),
    [
        (0, 500000, "57.000000"),
        (1, 150000, "56.200000"),
        (2, 125000, "56.130000"),
        (3, 123500, "56.124000"),
        (4, 123450, "56.123500"),
        (5, 123455, "56.123460"),
        (6, 123456, "56.123456"),
    ],
)
def test_temporal_values_interval_rounds_time_without_folding_days(
    precision, microseconds, seconds
):
    result = convert_clock_interval_value(
        dt.timedelta(days=-2, seconds=56, microseconds=microseconds),
        f"INTERVAL({precision})",
    )
    assert isinstance(result, IntervalTransport)
    assert result == f"-2 days +{seconds} seconds"
    assert temporal_family(result) == "interval"


@pytest.mark.parametrize(
    ("value", "native", "expected"),
    [
        (dt.time(23, 59, 59, 999999), "TIME(0)", "24:00:00.000000"),
        (
            dt.time(23, 59, 59, 999999, tzinfo=dt.timezone(dt.timedelta(hours=3))),
            "TIMETZ(0)",
            "24:00:00.000000+03:00:00",
        ),
        (dt.timedelta(microseconds=-1), "INTERVAL(0)", "-1 days +86400.000000 seconds"),
        (pd.Timedelta(-1, unit="ns"), "INTERVAL(6)", "-1 days +86400.000000 seconds"),
        (pd.Timedelta(1500, unit="ns"), "INTERVAL(6)", "0 days +0.000002 seconds"),
        (IntervalTransport("3 days -0.500000 seconds"), "INTERVAL(0)", "3 days -1.000000 seconds"),
        (
            IntervalTransport("0 days -0.000001500 seconds"),
            "INTERVAL(6)",
            "0 days -0.000002 seconds",
        ),
        (dt.timedelta.max, "INTERVAL", "999999999 days +86399.999999 seconds"),
    ],
)
def test_temporal_values_carry_and_nanoseconds_keep_exact_native_components(
    value, native, expected
):
    assert convert_clock_interval_value(value, native) == expected


def test_temporal_values_freezing_retains_nanoseconds_until_strict_or_explicit_resolution():
    source = pd.Timedelta(1, unit="ns")
    frozen = freeze_clock_interval_value(source, "INTERVAL(6)")
    assert type(frozen) is pd.Timedelta
    assert frozen.value == source.value == 1
    assert _profile(frozen).temporal_scale == 9
    with pytest.raises(ValueError, match="precision or fields"):
        validate_clock_interval_profile(
            _profile(frozen), "INTERVAL(6)", allow_timestamp_precision_loss=False
        )
    halfway = freeze_clock_interval_value(pd.Timedelta(1500, unit="ns"), "INTERVAL(6)")
    assert type(halfway) is pd.Timedelta
    assert halfway.value == 1500
    assert convert_clock_interval_value(halfway, "INTERVAL(6)") == "0 days +0.000002 seconds"
    negative = freeze_clock_interval_value(pd.Timedelta(-1, unit="ns"), "INTERVAL(6)")
    assert type(negative) is pd.Timedelta
    assert negative.value == -1
    assert negative.days == -1
    assert negative.seconds == 86399
    assert negative.microseconds == 999999
    assert negative.nanoseconds == 999
    assert convert_clock_interval_value(negative, "INTERVAL(6)") == "-1 days +86400.000000 seconds"


def test_temporal_values_freezing_owns_nested_clock_offsets_before_user_mutation():
    class MutableOffset(dt.tzinfo):
        offset = dt.timedelta(hours=3)
        name = "original-offset"

        def utcoffset(self, value):
            return self.offset

        def tzname(self, value):
            return self.name

    zone = MutableOffset()
    source = dt.time(1, 2, 3, 123456, tzinfo=zone, fold=1)
    original = [[source, None]]
    frozen = freeze_clock_interval_value(original, "TIMETZ(3)[][]")
    zone.offset = dt.timedelta(hours=-5)
    zone.name = "changed-offset"
    original[0][0] = dt.time(9)
    captured = frozen[0][0]
    assert type(captured) is dt.time
    assert captured.isoformat() == "01:02:03.123456+03:00"
    assert captured.utcoffset() == dt.timedelta(hours=3)
    assert captured.tzname() == "original-offset"
    assert captured.dst() is None
    assert captured.fold == 1
    assert captured.tzinfo is not zone
    assert frozen[0][1] is None
    assert source.tzinfo is zone
    assert source.utcoffset() == dt.timedelta(hours=-5)
    assert freeze_clock_interval_value(frozen, "TIMETZ(3)[][]") == frozen
    converted = convert_clock_interval_value(frozen[0][0], "TIMETZ(3)")
    assert converted == "01:02:03.123000+03:00:00"
    assert convert_clock_interval_value(converted, "TIMETZ(3)") == converted


@pytest.mark.parametrize("key", ["Owner/UnknownClockZone", "UTC"])
def test_temporal_values_named_clock_snapshot_keeps_observed_offset_when_name_cannot_supply_it(key):
    class NamedClock(dt.tzinfo):
        offset = dt.timedelta(hours=3)
        name = "observed-clock"
        calls = 0

        def utcoffset(self, value):
            self.calls += 1
            return self.offset

        def tzname(self, value):
            self.calls += 1
            return self.name

    zone = NamedClock()
    zone.key = key
    source = dt.time(1, 2, 3, 123456, tzinfo=zone, fold=1)
    frozen = freeze_clock_interval_value(source, "TIMETZ")
    captured_calls = zone.calls
    assert zone.offset == dt.timedelta(hours=3)
    assert zone.name == "observed-clock"
    zone.offset = dt.timedelta(hours=-5)
    zone.name = "mutated-clock"
    assert frozen.utcoffset() == dt.timedelta(hours=3)
    assert frozen.tzname() == "observed-clock"
    assert frozen.fold == 1
    assert frozen.tzinfo is not zone
    converted = convert_clock_interval_value(frozen, "TIMETZ")
    assert converted == "01:02:03.123456+03:00:00"
    assert adapt(converted).getquoted() == b"'01:02:03.123456+03:00:00'"
    if key != "UTC":
        with pytest.raises(ValueError, match="timezone name could not be resolved"):
            typed_text(frozen)
    else:
        with ReplaySpool(["clock"]) as spool:
            spool.write_rows([(frozen,)])
            spool.certify()
            from_spool = next(spool.iter_rows())[0]
        from_text = decode_value(json.loads(typed_text(frozen))["value"])
        for replayed in (from_spool, from_text):
            assert type(replayed) is dt.time
            assert replayed.isoformat() == "01:02:03.123456+03:00"
            assert replayed.utcoffset() == dt.timedelta(hours=3)
            assert replayed.tzname() == "observed-clock"
            assert replayed.fold == 1
            assert convert_clock_interval_value(replayed, "TIMETZ") == converted
    assert zone.calls == captured_calls
    assert source.tzinfo is zone
    assert source.fold == 1


def test_temporal_values_market_clock_label_survives_snapshot_and_exact_replay():
    class MarketClock(dt.tzinfo):
        key = "UTC"
        offset_calls = 0
        name_calls = 0

        def utcoffset(self, value):
            self.offset_calls += 1
            return dt.timedelta(0)

        def tzname(self, value):
            self.name_calls += 1
            return "MarketClock"

    zone = MarketClock()
    source = dt.time(1, 2, 3, 123456, tzinfo=zone, fold=1)
    frozen = freeze_clock_interval_value(source, "TIMETZ")
    assert zone.offset_calls == 1
    assert zone.name_calls == 1
    assert source.tzinfo is zone
    assert zone.key == "UTC"
    assert source.fold == 1
    assert frozen.tzname() == "MarketClock"
    assert frozen.utcoffset() == dt.timedelta(0)
    assert frozen.fold == 1
    with ReplaySpool(["clock"]) as spool:
        spool.write_rows([(frozen,)])
        spool.certify()
        from_spool = next(spool.iter_rows())[0]
    from_text = decode_value(json.loads(typed_text(frozen))["value"])
    for replayed in (from_spool, from_text):
        assert replayed.isoformat() == "01:02:03.123456+00:00"
        assert replayed.tzname() == "MarketClock"
        assert replayed.utcoffset() == dt.timedelta(0)
        assert replayed.fold == 1
    assert zone.offset_calls == 1
    assert zone.name_calls == 1


@pytest.mark.parametrize("value", [dt.timedelta.max, dt.timedelta(microseconds=-1)])
def test_temporal_values_freezing_preserves_native_duration_type_and_text(value):
    frozen = freeze_clock_interval_value(value, "INTERVAL")
    assert type(frozen) is type(value)
    assert frozen == value
    assert str(frozen) == str(value)
    assert (frozen.days, frozen.seconds, frozen.microseconds) == (
        value.days,
        value.seconds,
        value.microseconds,
    )


def test_temporal_values_freezing_preserves_named_time_zone_when_clock_has_no_offset():
    source = dt.time(1, 2, 3, 123456, tzinfo=ZoneInfo("Europe/Berlin"), fold=1)
    frozen = freeze_clock_interval_value(source, "TIME")
    assert type(frozen) is dt.time
    assert frozen == source
    assert frozen.fold == 1
    assert isinstance(frozen.tzinfo, ZoneInfo)
    assert frozen.tzinfo.key == "Europe/Berlin"
    assert frozen.utcoffset() is None
    assert source.tzinfo.key == "Europe/Berlin"
    assert infer_clock_interval_type(_profile(frozen)) is None
    replayed = decode_value(json.loads(typed_text(frozen))["value"])
    assert type(replayed) is dt.time
    assert replayed.tzinfo.key == "Europe/Berlin"
    assert replayed.fold == 1
    assert replayed == source


@pytest.mark.parametrize("offset", [dt.timedelta(hours=16), dt.timedelta(microseconds=1)])
def test_temporal_values_freezing_preserves_non_native_offsets_for_exact_text_fallback(offset):
    source = dt.time(1, 2, 3, 456789, tzinfo=dt.timezone(offset), fold=1)
    frozen = freeze_clock_interval_value(source, "TIME")
    assert type(frozen) is dt.time
    assert frozen.utcoffset() == offset
    assert frozen.fold == 1
    profile = _profile(frozen)
    assert not profile.native_constraints_valid
    assert infer_clock_interval_type(profile) is None
    with pytest.raises(ValueError, match="incompatible"):
        validate_clock_interval_profile(profile, "TIME")
    replayed = decode_value(json.loads(typed_text(frozen))["value"])
    assert replayed == source
    assert replayed.utcoffset() == offset
    assert replayed.fold == 1
    assert replayed.microsecond == 456789


@pytest.mark.parametrize(
    ("native", "values"),
    [
        ("TIME[]", [[dt.time(1), None], [dt.time(2), dt.time(3)]]),
        ("TIME[][]", [dt.time(1), None, dt.time(2)]),
    ],
)
def test_temporal_values_freezing_follows_actual_array_topology_instead_of_catalog_rank(
    native, values
):
    frozen = freeze_clock_interval_value(values, native)
    assert frozen == values
    assert frozen is not values
    assert values[0] is not None
    if isinstance(values[0], list):
        assert frozen[0] is not values[0]
        assert type(frozen[0][0]) is dt.time
        values[0][0] = dt.time(9)
        assert frozen[0][0] == dt.time(1)
    else:
        assert type(frozen[0]) is dt.time
        values[0] = dt.time(9)
        assert frozen[0] == dt.time(1)


@pytest.mark.parametrize(
    ("native", "message"),
    [("TIME[]", "arrays require array values"), ("VARCHAR", "supported Greenplum")],
)
def test_temporal_values_freezing_rejects_scalar_array_inputs_and_non_temporal_destinations(
    native, message
):
    with pytest.raises(ValueError, match=message):
        freeze_clock_interval_value(dt.time(1), native)


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        ("TIME(6)", "01:02:03.456789"),
        ("TIME(6) WITHOUT TIME ZONE", "01:02:03.456789"),
        ("TIMETZ(6)", "01:02:03.456789-04:00:15"),
        ("TIME(6) WITH TIME ZONE", "01:02:03.456789-04:00:15"),
    ],
)
def test_temporal_values_clock_timezone_conversion_keeps_wall_clock(native, expected):
    value = dt.time(1, 2, 3, 456789, tzinfo=dt.timezone(-dt.timedelta(hours=4, seconds=15)))
    assert convert_clock_interval_value(value, native) == expected


def test_temporal_values_naive_timetz_leaves_session_timezone_interpretation_to_server():
    value = dt.time(1, 2, 3, 456789)
    assert convert_clock_interval_value(value, "TIME WITH TIME ZONE") == "01:02:03.456789"
    profile = _profile(value)
    assert profile.naive
    assert not profile.aware
    assert validate_clock_interval_profile(profile, "TIMETZ(6)")


@pytest.mark.parametrize(
    "offset", [dt.timedelta(hours=16), -dt.timedelta(hours=16), dt.timedelta(microseconds=1)]
)
@pytest.mark.parametrize("native", ["TIME", "TIMETZ"])
def test_temporal_values_rejects_offsets_outside_native_second_precision(offset, native):
    value = dt.time(1, tzinfo=dt.timezone(offset))
    with pytest.raises(ValueError, match="offset"):
        convert_clock_interval_value(value, native)
    with pytest.raises(ValueError, match="incompatible"):
        validate_clock_interval_profile(_profile(value), native)


@pytest.mark.parametrize("sign", [-1, 1])
def test_temporal_values_timetz_accepts_native_offset_boundary(sign):
    value = dt.time(1, tzinfo=dt.timezone(sign * dt.timedelta(hours=15, minutes=59, seconds=59)))
    suffix = "-15:59:59" if sign < 0 else "+15:59:59"
    assert convert_clock_interval_value(value, "TIMETZ") == f"01:00:00.000000{suffix}"


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ("YEAR", "0 days +0.000000 seconds"),
        ("MONTH", "0 days +0.000000 seconds"),
        ("YEAR TO MONTH", "0 days +0.000000 seconds"),
        ("DAY", "2 days +0.000000 seconds"),
        ("HOUR", "2 days +3600.000000 seconds"),
        ("DAY TO HOUR", "2 days +3600.000000 seconds"),
        ("MINUTE", "2 days +3660.000000 seconds"),
        ("DAY TO MINUTE", "2 days +3660.000000 seconds"),
        ("HOUR TO MINUTE", "2 days +3660.000000 seconds"),
        ("SECOND(3)", "2 days +3661.124000 seconds"),
        ("DAY TO SECOND(3)", "2 days +3661.124000 seconds"),
        ("HOUR TO SECOND(3)", "2 days +3661.124000 seconds"),
        ("MINUTE TO SECOND(3)", "2 days +3661.124000 seconds"),
    ],
)
def test_temporal_values_interval_fields_discard_only_less_significant_components(fields, expected):
    value = dt.timedelta(days=2, hours=1, minutes=1, seconds=1, microseconds=123500)
    native = f"INTERVAL {fields}"
    profile = _profile(value)
    assert validate_clock_interval_profile(profile, native)
    assert convert_clock_interval_value(value, native) == expected
    with pytest.raises(ValueError, match="precision or fields"):
        validate_clock_interval_profile(profile, native, allow_timestamp_precision_loss=False)


@pytest.mark.parametrize(
    ("native", "value"),
    [
        ("TIME(3)", dt.time(1, microsecond=123456)),
        ("INTERVAL(3)", dt.timedelta(microseconds=123456)),
        ("INTERVAL(6)", pd.Timedelta(1, unit="ns")),
        ("INTERVAL HOUR", dt.timedelta(seconds=1)),
        ("INTERVAL HOUR", pd.Timedelta(1, unit="ns")),
        ("INTERVAL MONTH", dt.timedelta(days=1)),
    ],
)
def test_temporal_values_strict_profiles_reject_loss_while_explicit_conversion_is_allowed(
    native, value
):
    profile = _profile(value)
    assert validate_clock_interval_profile(profile, native)
    with pytest.raises(ValueError, match="precision or fields"):
        validate_clock_interval_profile(profile, native, allow_timestamp_precision_loss=False)


@pytest.mark.parametrize(
    ("native", "value"),
    [
        ("TIME(3)", dt.time(1, microsecond=123000)),
        ("INTERVAL(6)", pd.Timedelta(123456000, unit="ns")),
        ("INTERVAL HOUR", dt.timedelta(days=-2, hours=3)),
        ("INTERVAL DAY", dt.timedelta(days=2)),
        ("INTERVAL YEAR TO MONTH", dt.timedelta(0)),
    ],
)
def test_temporal_values_aligned_profiles_are_accepted_without_precision_loss(native, value):
    assert validate_clock_interval_profile(
        _profile(None, value, pd.NaT), native, allow_timestamp_precision_loss=False
    )


def test_temporal_values_profiles_aggregate_time_and_interval_facts_through_enums():
    class Clock(Enum):
        value = dt.time(1, microsecond=123000, tzinfo=dt.timezone.utc)

    profile = _profile(None, Clock.value, dt.time(2, microsecond=120000))
    assert profile.families == {"time"}
    assert profile.temporal_scale == 3
    assert profile.aware
    assert profile.naive
    assert profile.native_constraints_valid
    assert convert_clock_interval_value(Clock.value, "TIME(3)") == "01:00:00.123000"
    interval = _profile(dt.timedelta(days=2, hours=2), dt.timedelta(hours=3))
    assert interval.families == {"interval"}
    assert interval.interval_days_nonzero
    assert interval.interval_time_gcd == 3600 * 10**9


@pytest.mark.parametrize("native", ["TIME", "TIMETZ(3)", "INTERVAL", "INTERVAL DAY TO SECOND(6)"])
def test_temporal_values_null_only_profiles_are_valid_and_convert_to_none(native):
    assert is_gp_clock_interval_type(native)
    assert validate_clock_interval_profile(_profile(None, pd.NA, pd.NaT), native)
    for value in (None, pd.NA, pd.NaT):
        assert convert_clock_interval_value(value, native) is None


@pytest.mark.parametrize(
    ("native", "value"),
    [
        ("TIME", dt.timedelta(seconds=1)),
        ("INTERVAL", dt.time(1)),
        ("TIME", "01:00:00"),
        ("INTERVAL", 1.5),
        ("TIME", dt.date(2025, 1, 1)),
    ],
)
def test_temporal_values_wrong_families_fail_validation_and_conversion(native, value):
    with pytest.raises(ValueError, match="incompatible"):
        validate_clock_interval_profile(_profile(value), native)
    with pytest.raises(ValueError, match="incompatible"):
        convert_clock_interval_value(value, native)


@pytest.mark.parametrize(
    ("native", "value"),
    [
        ("TIME", TimeTransport("24:00:00.000001")),
        ("INTERVAL", IntervalTransport(f"{2**31} days +0.000000 seconds")),
    ],
)
def test_temporal_values_native_component_overflow_cannot_pass_profile_validation(native, value):
    profile = _profile(value)
    assert not profile.native_constraints_valid
    with pytest.raises(ValueError, match="incompatible"):
        validate_clock_interval_profile(profile, native)


@pytest.mark.parametrize(
    ("native", "value"),
    [
        ("TIME", TimeTransport("01:02:03.12345")),
        ("INTERVAL", IntervalTransport("1 day +1.000000 seconds")),
    ],
)
def test_temporal_values_malformed_transport_literals_cannot_be_certified_or_bound(native, value):
    profile = _profile(value)
    assert not profile.native_constraints_valid
    with pytest.raises(ValueError, match="incompatible"):
        validate_clock_interval_profile(profile, native)
    with pytest.raises(ValueError, match="incompatible"):
        freeze_clock_interval_value(value, native)
    with pytest.raises(ValueError, match="incompatible"):
        convert_clock_interval_value(value, native)


@pytest.mark.parametrize("native", ["TIME(7)", "TIMETZ(9)", "INTERVAL(7)"])
def test_temporal_values_invalid_native_precision_fails_before_binding(native):
    with pytest.raises(ValueError, match=r"precision.*between 0 and 6"):
        validate_clock_interval_profile(_profile(), native)
    with pytest.raises(ValueError, match="supported Greenplum"):
        convert_clock_interval_value(dt.time(1), native)


@pytest.mark.parametrize("native", ["INTERVAL DAY(3)", "INTERVAL YEAR TO MONTH(2)"])
def test_temporal_values_interval_precision_requires_second_field(native):
    with pytest.raises(ValueError, match=r"SECOND|Invalid parameters.*Greenplum"):
        validate_clock_interval_profile(_profile(), native)


@pytest.mark.parametrize("native", ["TIME[]", "TIMESTAMP", "INTERVAL WEEK", '"application"."time"'])
def test_temporal_values_non_builtin_destinations_do_not_match(native):
    assert not is_gp_clock_interval_type(native)
    assert not validate_clock_interval_profile(_profile(), native)
    with pytest.raises(ValueError, match="supported Greenplum"):
        convert_clock_interval_value(dt.time(1), native)


def test_temporal_values_real_psycopg_batch_adaptation_retains_offsets_days_nulls_and_arrays():
    clock = dt.time(
        1, 2, 3, 456789, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30, seconds=45))
    )
    unchanged_driver_clock = adapt(clock).getquoted()
    converted_clock = convert_clock_interval_value(clock, "TIMETZ(6)")
    maximum = convert_clock_interval_value(dt.timedelta.max, "INTERVAL(6)")
    negative = convert_clock_interval_value(dt.timedelta(microseconds=-1), "INTERVAL(0)")
    midnight = convert_clock_interval_value(dt.time(23, 59, 59, 999999), "TIME(0)")
    frame = pd.DataFrame(
        {
            "clock": pd.Series([converted_clock, None, "01:02:03.000000"], dtype=object),
            "elapsed": pd.Series([maximum, None, negative], dtype=object),
            "clocks": pd.Series([[midnight, None], None, [None, midnight]], dtype=object),
            "elapsed_grid": pd.Series([[[negative, None]], None, [[None, negative]]], dtype=object),
        }
    )
    connection = AdaptedConnection()
    get_backend("gp").insert_dataframe_batch(
        connection,
        '"sandbox"."native_temporals"',
        frame,
        target_column_types={
            "clock": "TIMETZ(6)",
            "elapsed": "INTERVAL(6)",
            "clocks": "TIME(0)[]",
            "elapsed_grid": "INTERVAL(0)[][]",
        },
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    statement = connection.target.statements[0]
    assert b"'01:02:03.456789+05:30:45'" in statement
    assert b"'999999999 days +86399.999999 seconds'" in statement
    assert b"(NULL, NULL, CAST(NULL AS TIME(0)[]), CAST(NULL AS INTERVAL(0)[][]))" in statement
    assert b"CAST(ARRAY['24:00:00.000000',NULL] AS TIME(0)[])" in statement
    assert (
        b"CAST(ARRAY[ARRAY['-1 days +86400.000000 seconds',NULL]] AS INTERVAL(0)[][])" in statement
    )
    assert connection.committed
    assert connection.target.closed
    assert adapt(clock).getquoted() == unchanged_driver_clock
