"""Exact native Trino clock precision and transport boundaries."""

from __future__ import annotations

import datetime as dt
from enum import Enum

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit.sql.backends.trino import time_values as clocks
from analytics_toolkit.sql.backends.trino.parameters import normalize_value
from analytics_toolkit.sql.backends.type_resolution import ResolvedColumn, validate_profile
from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
from analytics_toolkit.sql.core.exact_values import decode_value, encode_value, typed_text
from analytics_toolkit.sql.core.replay_spool import ReplaySpool
from sqlglot.errors import ParseError
from zoneinfo import ZoneInfo

from tests.sql._support.trino_parameters import render_parameter


def profile(*values):
    result = ColumnProfile()
    for value in values:
        result.add(value)
    return result


@pytest.mark.parametrize("precision", range(13))
@pytest.mark.parametrize("suffix", ["", " WITH TIME ZONE", " WITHOUT TIME ZONE"])
def test_native_precision_domain_and_strict_aligned_values(precision, suffix):
    native = f"TIME({precision}){suffix}"
    value = dt.time(23, 59, 59)
    assert clocks.is_time_type(native)
    assert clocks.validate_time_profile(
        profile(None, value), native, allow_timestamp_precision_loss=False
    )
    assert ResolvedColumn(native, target_backend="trino").convert(value) == value


@pytest.mark.parametrize("native", ["TIME(13)", "TIME(999) WITH TIME ZONE"])
def test_precision_outside_native_domain_rejected(native):
    with pytest.raises(ValueError, match="between 0 and 12"):
        clocks.validate_time_profile(profile(dt.time()), native)


@pytest.mark.parametrize("native", ["TIME(-1)", "TIME(-0)", "TIME(+3)"])
def test_signed_precision_is_not_valid_trino_type_syntax(native):
    assert not clocks.is_time_type(native)
    with pytest.raises(ValueError, match="precision"):
        validate_profile(profile(dt.time()), native, "trino")
    with pytest.raises(ValueError, match="between 0 and 12"):
        clocks.convert_time_value(dt.time(), native)


@pytest.mark.parametrize("native", ["TIME", "TIME WITH TIME ZONE", "TIME WITHOUT TIME ZONE"])
def test_bare_time_defaults_to_milliseconds(native):
    value = dt.time(1, 2, 3, 123500)
    assert clocks.convert_time_value(value, native) == dt.time(1, 2, 3, 124000)
    with pytest.raises(ValueError, match="precision"):
        clocks.validate_time_profile(profile(value), native, allow_timestamp_precision_loss=False)


@pytest.mark.parametrize("precision", [0, 1, 2, 3, 4, 5])
def test_explicit_rounding_is_half_up_with_midnight_wrap(precision):
    native = f"TIME({precision})"
    assert clocks.convert_time_value(dt.time(23, 59, 59, 999999), native) == dt.time()
    unit = 10 ** (6 - precision)
    expected = dt.time(second=1) if precision == 0 else dt.time(microsecond=unit)
    assert clocks.convert_time_value(dt.time(microsecond=unit // 2), native) == expected


@pytest.mark.parametrize(("hours", "minutes"), [(0, 0), (14, 0), (-14, 0), (5, 30), (-3, -30)])
def test_captured_offset_uses_sdk_numeric_zone_including_utc(hours, minutes):
    offset = dt.timedelta(hours=hours, minutes=minutes)
    value = dt.time(2, 3, 4, 123456, tzinfo=dt.timezone(offset, "custom timezone"))
    actual = clocks.convert_time_value(value, "TIME(6) WITH TIME ZONE")
    assert actual.utcoffset() == offset
    assert actual.hour == value.hour
    total_minutes = int(offset.total_seconds()) // 60
    hour, minute = divmod(abs(total_minutes), 60)
    expected = f"{'-' if total_minutes < 0 else '+'}{hour:02d}:{minute:02d}"
    assert render_parameter(actual) == f"TIME '02:03:04.123456 {expected}'"


@pytest.mark.parametrize(
    "offset",
    [dt.timedelta(hours=14, minutes=1), dt.timedelta(hours=-15), dt.timedelta(seconds=1)],
)
def test_invalid_offset_is_rejected_by_complete_profile_and_conversion(offset):
    value = dt.time(tzinfo=dt.timezone(offset))
    with pytest.raises(ValueError, match="whole minutes"):
        clocks.convert_time_value(value, "TIME(6)")
    with pytest.raises(ValueError, match="fixed whole-minute"):
        validate_profile(profile(value), "TIME(6)", "trino")


@pytest.mark.parametrize(
    "value", [dt.date(2026, 1, 1), dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), 1]
)
def test_wrong_native_family_is_rejected(value):
    with pytest.raises(TypeError, match="Trino TIME values"):
        clocks.convert_time_value(value, "TIME(6)")
    with pytest.raises(ValueError, match="Trino TIME values"):
        validate_profile(profile(value), "TIME(6)", "trino")


def test_native_timezone_crosscasts_retain_input_for_server_policy():
    naive = dt.time(8, 30)
    aware = dt.time(8, 30, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert clocks.convert_time_value(naive, "TIME(6) WITH TIME ZONE").tzinfo is None
    assert clocks.convert_time_value(aware, "TIME(6)").utcoffset() == dt.timedelta(hours=2)


class MutableOffset(dt.tzinfo):
    calls = 0

    def utcoffset(self, _value):
        self.calls += 1
        return dt.timedelta(hours=self.calls)

    def tzname(self, _value):
        return "mutable offset"


def test_preparation_freezes_callback_once_and_preserves_caller():
    zone = MutableOffset()
    value = dt.time(1, 2, tzinfo=zone)
    frame = pd.DataFrame({"clock": pd.Series([value], dtype=object), "other": [3]})
    prepared = clocks.prepare_dataframe_payload(
        None, frame, {"clock": "TIME(6)", "other": "BIGINT"}
    )
    assert zone.calls == 1
    first = clocks.convert_time_value(prepared.iloc[0, 0], "TIME(6)")
    second = clocks.convert_time_value(prepared.iloc[0, 0], "TIME(6)")
    assert render_parameter(first) == render_parameter(second) == "TIME '01:02:00.000000 +01:00'"
    assert zone.calls == 1
    assert frame.iloc[0, 0] is value
    assert clocks.prepare_dataframe_payload(None, frame, {"clock": "VARCHAR"}) is None


def test_untyped_clock_snapshot_preserves_text_domain_and_named_zone():
    for zone in [
        dt.timezone(dt.timedelta(hours=18)),
        dt.timezone(dt.timedelta(microseconds=1)),
        ZoneInfo("America/New_York"),
    ]:
        value = dt.time(12, 30, 45, 123456, tzinfo=zone, fold=1)
        frame = pd.DataFrame({"clock": pd.Series([None, value], dtype=object)})
        prepared = clocks.prepare_dataframe_payload(None, frame, None)
        actual = prepared.iloc[1, 0]
        assert encode_value(actual) == encode_value(value)
        assert encode_value(decode_value(encode_value(actual))) == encode_value(value)
        assert frame.iloc[1, 0] is value
        assert actual.fold == 1


def test_untyped_preparation_leaves_ordinary_or_mixed_columns_alone():
    for values in [[1, 2], [None, None], [dt.time(), "clock"]]:
        assert clocks.prepare_dataframe_payload(None, pd.DataFrame({"x": values}), None) is None


def test_nested_preparation_retains_topology_and_caller_identity():
    value = dt.time(1, tzinfo=dt.timezone.utc)
    native = "ROW(clock TIME(6), clocks ARRAY(TIME(6)), lookup MAP(VARCHAR, TIME(6)))"
    row = {"clock": value, "clocks": [value, None], "lookup": {"a": value}}
    frame = pd.DataFrame({"payload": pd.Series([row], dtype=object)})
    prepared = clocks.prepare_dataframe_payload(None, frame, {"payload": native})
    actual = prepared.iloc[0, 0]
    assert actual["clock"].utcoffset() == dt.timedelta()
    assert actual["clocks"][1] is None
    assert next(iter(actual["lookup"].items()))[0] == "a"
    assert frame.iloc[0, 0] is row
    assert row["clocks"][0] is value


@pytest.mark.parametrize("row_factory", [tuple, list])
def test_positional_row_clocks_reach_real_sdk_without_float_or_timezone_loss(row_factory):
    zone = MutableOffset()
    clock = dt.time(12, 30, 45, 123456, tzinfo=zone)
    row = row_factory([clock, 2**53 + 1])
    native = "ROW(clock TIME(6) WITH TIME ZONE, quantity BIGINT)"
    frame = pd.DataFrame({"payload": pd.Series([row], dtype=object)})

    prepared = clocks.prepare_dataframe_payload(None, frame, {"payload": native})
    actual = normalize_value(prepared.iloc[0, 0], native)

    assert render_parameter(actual) == "ROW('12:30:45.123456+01:00',9007199254740993)"
    assert zone.calls == 1
    assert frame.iloc[0, 0] is row
    assert row[0] is clock


class NamedFixedOffset(dt.tzinfo):
    def __init__(self, key, *, offset=None, name="UTC+03:00"):
        self.key = key
        self.calls = 0
        self.offset = dt.timedelta(hours=3) if offset is None else offset
        self.name = name

    def utcoffset(self, _value):
        self.calls += 1
        return self.offset

    def tzname(self, _value):
        return self.name

    def dst(self, _value):
        return None


@pytest.mark.parametrize("key", ["Unknown/Private_Clock_Zone", "UTC"])
def test_clock_snapshot_uses_captured_offset_for_native_binding_with_unverified_zone_key(key):
    zone = NamedFixedOffset(key)
    clock = dt.time(12, 30, 45, 123456, tzinfo=zone)
    frame = pd.DataFrame({"clock": pd.Series([clock], dtype=object)})
    frozen = clocks.prepare_dataframe_payload(None, frame, None)
    snapshot = frozen.iloc[0, 0]

    assert snapshot.utcoffset() == dt.timedelta(hours=3)
    assert snapshot.dst() is None
    assert snapshot.tzname() == "UTC+03:00"
    native = clocks.prepare_dataframe_payload(None, frozen, {"clock": "TIME(6) WITH TIME ZONE"})
    assert render_parameter(normalize_value(native.iloc[0, 0], "TIME(6) WITH TIME ZONE")) == (
        "'12:30:45.123456+03:00'"
    )
    assert zone.calls == 1
    assert frame.iloc[0, 0] is clock
    if key == "Unknown/Private_Clock_Zone":
        with ReplaySpool(["clock"]) as spool:
            with pytest.raises(ValueError, match="timezone name could not be resolved"):
                spool.write_rows([(snapshot,)])
            assert spool.closed
        assert not spool.path.exists()


def test_clock_snapshot_spool_and_text_roundtrip_cannot_replace_captured_offset_with_a_zone_label():
    zone = NamedFixedOffset("UTC")
    clock = dt.time(12, 30, 45, 123456, tzinfo=zone, fold=1)
    frame = pd.DataFrame({"clock": pd.Series([clock], dtype=object)})
    snapshot = clocks.prepare_dataframe_payload(None, frame, None).iloc[0, 0]

    expected_text = typed_text(snapshot)
    with ReplaySpool(["clock"]) as spool:
        spool.write_rows([(None,), (snapshot,), (None,)])
        spool.certify()
        rows = list(spool.iter_rows())
        assert rows[0] == rows[2] == (None,)
        restored = rows[1][0]
        actual = (
            restored.utcoffset(),
            render_parameter(normalize_value(restored, "TIME(6) WITH TIME ZONE")),
            typed_text(restored),
        )

    assert actual == (
        dt.timedelta(hours=3),
        "'12:30:45.123456+03:00'",
        expected_text,
    )
    assert restored.hour == clock.hour
    assert restored.fold == 1
    assert zone.calls == 1
    assert not spool.path.exists()


def test_clock_snapshot_retains_captured_timezone_name_when_only_named_offset_matches():
    label = "custom UTC clock"
    zone = NamedFixedOffset("UTC", offset=dt.timedelta(), name=label)
    clock = dt.time(12, 30, 45, 123456, tzinfo=zone)
    frame = pd.DataFrame({"clock": pd.Series([clock], dtype=object)})
    snapshot = clocks.prepare_dataframe_payload(None, frame, None).iloc[0, 0]
    expected = dt.time(12, 30, 45, 123456, tzinfo=dt.timezone(dt.timedelta(), label))

    with ReplaySpool(["clock"]) as spool:
        spool.write_rows([(snapshot,)])
        spool.certify()
        restored = next(spool.iter_rows())[0]
        actual = (
            snapshot.tzname(),
            restored.tzname(),
            typed_text(restored),
            render_parameter(normalize_value(restored, "TIME(6) WITH TIME ZONE")),
        )

    assert actual == (label, label, typed_text(expected), "'12:30:45.123456+00:00'")
    assert zone.calls == 1
    assert frame.iloc[0, 0] is clock
    assert not spool.path.exists()


def test_preparation_does_not_drop_invalid_row_members():
    native = "ROW(clock TIME(6))"
    for row in [{"clock": dt.time(), "extra": 1}, (dt.time(), 1)]:
        frame = pd.DataFrame({"payload": pd.Series([row], dtype=object)})
        actual = clocks.prepare_dataframe_payload(None, frame, {"payload": native}).iloc[0, 0]
        assert len(actual) == 2


def test_native_numpy_container_clocks_are_frozen_before_profiling():
    zone = MutableOffset()
    value = dt.time(1, 2, tzinfo=zone)
    array = np.array([value, None], dtype=object)
    frame = pd.DataFrame({"payload": pd.Series([array], dtype=object)})
    actual = clocks.prepare_dataframe_payload(None, frame, {"payload": "ARRAY(TIME(6))"})
    assert actual.iloc[0, 0][1] is None
    assert render_parameter(actual.iloc[0, 0][0]) == "TIME '01:02:00.000000 +01:00'"
    assert zone.calls == 1
    assert frame.iloc[0, 0] is array


def test_enum_null_clock_and_native_container_values_keep_logical_identity():
    class Values(Enum):
        NULL = None
        CLOCK = dt.time(12, 30, 45, 123456)

    class Containers(Enum):
        CLOCKS = (Values.CLOCK, Values.NULL)

    for schema in [None, {"clock": "TIME(6)"}]:
        frame = pd.DataFrame({"clock": pd.Series([Values.NULL, Values.CLOCK], dtype=object)})
        prepared = clocks.prepare_dataframe_payload(None, frame, schema)
        assert prepared.iloc[0, 0] is None
        assert prepared.iloc[1, 0] == Values.CLOCK.value
    frame = pd.DataFrame({"clocks": pd.Series([Containers.CLOCKS], dtype=object)})
    prepared = clocks.prepare_dataframe_payload(None, frame, {"clocks": "ARRAY(TIME(6))"})
    assert prepared.iloc[0, 0] == [Values.CLOCK.value, None]
    assert clocks.convert_time_value(Values.NULL, "TIME(6)") is None


@pytest.mark.parametrize(
    "native",
    [
        "ARRAY(TIME(13))",
        "ARRAY(TIME(+3))",
        "ARRAY(TIME(-0))",
        "ROW(good TIME(6), bad TIME(13))",
        "ROW(bad TIME(+3))",
        "MAP(VARCHAR, TIME(13))",
        "TIME(6)[]",
        "TUPLE(TIME(6))",
    ],
)
@pytest.mark.parametrize("value", [None, []])
def test_empty_nested_values_cannot_conceal_invalid_clock_declarations(native, value):
    frame = pd.DataFrame({"clock": pd.Series([value], dtype=object)})
    with pytest.raises((ValueError, ParseError), match=r"precision|Failed to parse|ARRAY"):
        clocks.prepare_dataframe_payload(None, frame, {"clock": native})
    with pytest.raises((ValueError, ParseError), match=r"precision|Failed to parse|ARRAY"):
        validate_profile(profile(value), native, "trino")


@pytest.mark.parametrize("value", [None, []])
def test_valid_empty_array_clock_declarations_still_validate(value):
    frame = pd.DataFrame({"clock": pd.Series([value], dtype=object)})
    prepared = clocks.prepare_dataframe_payload(None, frame, {"clock": "ARRAY(TIME(6))"})
    assert prepared.iloc[0, 0] == value
    validate_profile(profile(value), "ARRAY(TIME(6))", "trino")


@pytest.mark.parametrize("native", ["TIME(6)", "ARRAY(TIME(6))", "MAP(VARCHAR, TIME(6))"])
def test_unsupported_parquet_clock_types_fail_with_transport_guidance(native):
    with pytest.raises(ValueError, match="Parquet/Hive staging does not support TIME"):
        clocks.validate_staging_schema(None, {"clock": native}, use_parquet=True)
    clocks.validate_staging_schema(None, {"clock": native}, use_parquet=False)
    clocks.validate_staging_schema(None, {"amount": "BIGINT"}, use_parquet=True)


def test_profile_merge_retains_invalid_clock_coverage():
    good = profile(dt.time())
    bad = profile(dt.time(tzinfo=dt.timezone(dt.timedelta(seconds=1))))
    good.merge(bad)
    with pytest.raises(ValueError, match="Trino TIME values"):
        clocks.validate_time_profile(good, "TIME(6)")


def test_parquet_guard_uses_supported_existing_hive_mappings():
    clocks.validate_staging_schema(
        None,
        {"id": "UUID", "at": "TIMESTAMP(9) WITH TIME ZONE", "row": "ARRAY(UUID)"},
        use_parquet=True,
    )
    with pytest.raises(ValueError, match="Unsupported resolved Trino Parquet stage type"):
        clocks.validate_staging_schema(None, {"unsupported": "IPADDRESS"}, use_parquet=True)


def test_unrelated_types_are_not_claimed_and_null_is_preserved():
    assert not clocks.is_time_type("INTERVAL DAY TO SECOND")
    assert not clocks.validate_time_profile(profile(1), "BIGINT")
    assert clocks.convert_time_value(None, "TIME(6)") is None
