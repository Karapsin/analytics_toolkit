"""Trino native clocks retain exact ticks through casts and driver parameters."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from analytics_toolkit.sql.backends import get_backend
from analytics_toolkit.sql.backends.trino import insert
from analytics_toolkit.sql.backends.trino import time_values as clocks
from analytics_toolkit.sql.backends.trino.parameters import literal, normalize_value
from analytics_toolkit.sql.backends.type_resolution import ResolvedColumn, validate_profile
from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
from analytics_toolkit.sql.core.exact_values import (
    ExactClock,
    ExactTemporal,
    OrderedMapPairs,
    decode_value,
)
from analytics_toolkit.sql.core.replay_spool import ReplaySpool

from tests.sql._support.trino_parameters import DriverParameterConnection, render_parameter


def _profile(*values):
    result = ColumnProfile()
    for value in values:
        result.add(value)
    return result


def _ticks(value, precision):
    if isinstance(value, ExactClock):
        return value.ticks_floor(precision)
    seconds = value.hour * 3600 + value.minute * 60 + value.second
    return seconds * 10**precision + value.microsecond * 10**precision // 10**6


@pytest.mark.parametrize("precision", range(13))
@pytest.mark.parametrize("offset", [None, 0, 330])
def test_every_native_precision_retains_the_complete_clock(precision, offset):
    ticks = 3723 * 10**precision + (1 if precision else 0)
    value = ExactClock(ticks, precision, offset)
    native = f"TIME({precision})" + (" WITH TIME ZONE" if offset is not None else "")

    validate_profile(_profile(None, value), native, "trino", allow_timestamp_precision_loss=False)
    actual = ResolvedColumn(native, target_backend="trino").convert(value)

    assert _ticks(actual, precision) == ticks
    if precision > 6:
        assert isinstance(actual, ExactClock)
        assert actual.offset_minutes == offset
    else:
        assert isinstance(actual, dt.time)
        assert actual.utcoffset() == (None if offset is None else dt.timedelta(minutes=offset))
    assert value == ExactClock(ticks, precision, offset)


@pytest.mark.parametrize("precision", range(7, 13))
def test_microsecond_aligned_exact_values_use_native_python_clocks(precision):
    value = ExactClock(12_345_678 * 10 ** (precision - 6), precision, -210)
    profile = _profile(value)
    validate_profile(
        profile, "TIME(6) WITH TIME ZONE", "trino", allow_timestamp_precision_loss=False
    )

    actual = clocks.convert_time_value(value, "TIME(12) WITH TIME ZONE")

    assert isinstance(actual, dt.time)
    assert actual.replace(tzinfo=None) == dt.time(second=12, microsecond=345678)
    assert actual.utcoffset() == dt.timedelta(minutes=-210)
    assert render_parameter(actual) == "TIME '00:00:12.345678 -03:30'"


@pytest.mark.parametrize("precision", [0, 3, 6, 9, 11])
def test_explicit_exact_clock_rounding_is_half_up_and_wraps_midnight(precision):
    quantum = 10 ** (12 - precision)
    native = f"TIME({precision}) WITH TIME ZONE"
    below = ExactClock(quantum // 2 - 1, 12, 330)
    halfway = ExactClock(quantum // 2, 12, 330)
    midnight = ExactClock(86400 * 10**12 - quantum // 2, 12, 330)

    assert _ticks(clocks.convert_time_value(below, native), precision) == 0
    assert _ticks(clocks.convert_time_value(halfway, native), precision) == 1
    wrapped = clocks.convert_time_value(midnight, native)
    assert _ticks(wrapped, precision) == 0
    assert wrapped.utcoffset() == dt.timedelta(minutes=330)
    with pytest.raises(ValueError, match="precision"):
        validate_profile(_profile(halfway), native, "trino", allow_timestamp_precision_loss=False)


@pytest.mark.parametrize("offset", [-840, -210, 0, 330, 840])
def test_picosecond_parameters_and_literals_use_exact_typed_sdk_binding(offset):
    value = ExactClock(3723 * 10**12 + 123456789012, 12, offset)
    native = "TIME(12) WITH TIME ZONE"
    connection = DriverParameterConnection()

    insert.insert_rows(
        get_backend("trino"),
        connection,
        "sandbox.clocks",
        ["clock"],
        [(value,)],
        target_column_types={"clock": native},
        trino_insert_chunk_size=1,
    )

    statement, parameters, rendered = connection.statements[0]
    assert parameters == [value.isoformat()]
    assert rendered == [f"'{value.isoformat()}'"]
    assert f"CAST(? AS {native})" in statement
    assert literal(value, native) == f"CAST('{value.isoformat()}' AS {native})"
    assert connection.cursor_close_calls == 1


@pytest.mark.parametrize("offset", [-841, 841])
def test_outside_native_fixed_offset_rejects_before_any_sdk_submission(offset):
    value = ExactClock(1, 12, offset)
    native = "TIME(12) WITH TIME ZONE"
    connection = DriverParameterConnection()

    with pytest.raises(ValueError, match=r"whole.minute|14:00"):
        clocks.convert_time_value(value, native)
    with pytest.raises(ValueError, match="fixed whole-minute"):
        validate_profile(_profile(value), native, "trino")
    with pytest.raises(ValueError, match=r"whole.minute|14:00"):
        insert.insert_rows(
            get_backend("trino"),
            connection,
            "sandbox.clocks",
            ["clock"],
            [(value,)],
            target_column_types={"clock": native},
            trino_insert_chunk_size=1,
        )
    assert not connection.statements


def test_nested_row_array_and_map_values_reach_the_real_sdk_without_precision_loss():
    first = ExactClock(1, 12, 0)
    second = ExactClock(2, 12, 330)
    native = (
        "ROW(clock TIME(12) WITH TIME ZONE, clocks ARRAY(TIME(12) WITH TIME ZONE), "
        "lookup MAP(VARCHAR, TIME(12) WITH TIME ZONE))"
    )
    source = (first, [second, None], {"first": first, "second": second})

    normalized = normalize_value(source, native)

    assert normalized == (
        first.isoformat(),
        [second.isoformat(), None],
        {"first": first.isoformat(), "second": second.isoformat()},
    )
    assert render_parameter(normalized) == (
        "ROW('00:00:00.000000000001+00:00',"
        "ARRAY['00:00:00.000000000002+05:30',NULL],"
        "MAP(ARRAY['first','second'], "
        "ARRAY['00:00:00.000000000001+00:00','00:00:00.000000000002+05:30']))"
    )
    assert source[0] is first
    assert source[1][0] is second


@pytest.mark.parametrize("row", [False, True])
def test_picosecond_keys_stay_distinct_until_a_declared_lower_precision_cast(row):
    values = [ExactClock(ticks, 12) for ticks in (1, 2)]
    keys = [(value,) for value in values] if row else values
    source = OrderedMapPairs(tuple(zip(keys, ["first", "second"])))
    key_type = "ROW(clock TIME(12))" if row else "TIME(12)"
    native = f"MAP({key_type}, VARCHAR)"

    converted = ResolvedColumn(native, target_backend="trino").convert(source)
    normalized = normalize_value(converted, native)

    assert len(normalized) == 2
    assert list(normalized.values()) == ["first", "second"]
    assert "00:00:00.000000000001" in render_parameter(normalized)
    assert "00:00:00.000000000002" in render_parameter(normalized)
    with pytest.raises(ValueError, match="Converted map keys are not unique"):
        ResolvedColumn(native.replace("TIME(12)", "TIME(9)"), target_backend="trino").convert(
            source
        )


@pytest.mark.parametrize("row", [False, True])
def test_zoned_picosecond_key_equality_uses_utc_modulo_day_at_destination_precision(row):
    values = [ExactClock(30 * 60 * 10**12 + 1, 12, 60), ExactClock(84600 * 10**12 + 1, 12, 0)]
    keys = [(value,) for value in values] if row else values
    source = OrderedMapPairs(tuple(zip(keys, ["first", "second"])))
    key_type = "ROW(clock TIME(12) WITH TIME ZONE)" if row else "TIME(12) WITH TIME ZONE"

    with pytest.raises(ValueError, match="Converted map keys are not unique"):
        ResolvedColumn(f"MAP({key_type}, VARCHAR)", target_backend="trino").convert(source)


def test_plain_map_keys_keep_sql_distinct_walls_and_zoned_keys_keep_original_offsets():
    first = ExactClock(12 * 3600 * 10**12 + 1, 12, 0)
    second = ExactClock(13 * 3600 * 10**12 + 2, 12, 60)
    source = OrderedMapPairs(((first, "first"), (second, "second")))

    plain_native = "MAP(TIME(12), VARCHAR)"
    plain = ResolvedColumn(plain_native, target_backend="trino").convert(source)
    assert list(normalize_value(plain, plain_native)) == [
        "12:00:00.000000000001",
        "13:00:00.000000000002",
    ]
    zoned_native = "MAP(TIME(12) WITH TIME ZONE, VARCHAR)"
    zoned = ResolvedColumn(zoned_native, target_backend="trino").convert(source)
    assert list(normalize_value(zoned, zoned_native)) == [first.isoformat(), second.isoformat()]


def test_naive_exact_zoned_map_keys_require_an_explicit_offset():
    source = OrderedMapPairs(((ExactClock(1, 12), "naive"), (ExactClock(2, 12, 0), "aware")))
    with pytest.raises(ValueError, match=r"offset|timezone|session"):
        ResolvedColumn("MAP(TIME(12) WITH TIME ZONE, VARCHAR)", target_backend="trino").convert(
            source
        )


def test_timestamp_map_picoseconds_remain_distinct_in_native_payload_and_sdk_parameters():
    first, second = (ExactTemporal(ticks, 12, "UTC") for ticks in (1, 2))
    native = "MAP(TIMESTAMP(12) WITH TIME ZONE, VARCHAR)"
    source = OrderedMapPairs(((first, "first"), (second, "second")))

    converted = ResolvedColumn(native, target_backend="trino").convert(source)
    normalized = normalize_value(converted, native)

    assert len(converted) == len(normalized) == 2
    assert all(isinstance(key, ExactTemporal) for key in converted)
    assert list(normalized.items()) == [
        ("1970-01-01 00:00:00.000000000001 UTC", "first"),
        ("1970-01-01 00:00:00.000000000002 UTC", "second"),
    ]
    assert render_parameter(normalized) == (
        "MAP(ARRAY['1970-01-01 00:00:00.000000000001 UTC',"
        "'1970-01-01 00:00:00.000000000002 UTC'], ARRAY['first','second'])"
    )


def test_timestamp_map_native_tick_scale_aliases_remain_duplicate_keys():
    source = OrderedMapPairs(
        ((ExactTemporal(1, 0, "UTC"), "seconds"), (ExactTemporal(1000, 3, "UTC"), "milliseconds"))
    )
    with pytest.raises(ValueError, match="Converted map keys are not unique"):
        ResolvedColumn(
            "MAP(TIMESTAMP(12) WITH TIME ZONE, VARCHAR)", target_backend="trino"
        ).convert(source)


def test_plain_timestamp_map_picosecond_payload_marks_its_calendar_provenance():
    value = ExactTemporal(1, 12)
    native = "MAP(TIMESTAMP(12), VARCHAR)"
    conversion = ResolvedColumn(native, target_backend="trino")

    converted = conversion.convert({value: "value"})
    repeated = conversion.convert(converted)

    key = next(iter(converted))
    assert isinstance(key, ExactTemporal)
    assert key.wall_time
    assert key.ticks == 1
    assert key.scale == 12
    assert next(iter(repeated)) == key
    assert normalize_value(repeated, native) == {"1970-01-01 00:00:00.000000000001": "value"}
    assert not value.wall_time


@pytest.mark.parametrize(
    ("value", "clock"),
    [
        (ExactTemporal(1, 12, "UTC"), "00:00:00"),
        (ExactTemporal(1, 12, wall_time=True), "05:00:00"),
    ],
)
def test_picosecond_timestamp_map_frozen_context_is_applied_only_once(value, clock):
    native = "MAP(TIMESTAMP(12), VARCHAR)"
    conversion = ResolvedColumn(
        native, target_backend="trino", destination_timezone="America/New_York"
    )

    converted = conversion.convert({value: "value"})
    repeated = conversion.convert(converted)

    assert len(converted) == len(repeated) == 1
    assert next(iter(converted)) == next(iter(repeated))
    expected = f"1970-01-01 {clock}.000000000001 UTC"
    for version in (converted, repeated):
        assert normalize_value(version, native) == {expected: "value"}
        assert f"'{expected}'" in render_parameter(normalize_value(version, native))


def test_untyped_exact_timestamp_literals_keep_all_twelve_fractional_digits():
    assert literal(ExactTemporal(1, 12), None) == ("TIMESTAMP '1970-01-01 00:00:00.000000000001'")
    assert literal(ExactTemporal(-1, 12, "UTC"), None) == (
        "TIMESTAMP '1969-12-31 23:59:59.999999999999 UTC'"
    )


@pytest.mark.parametrize("offset", [None, 0, 330])
def test_exact_clock_text_parameters_and_literals_retain_the_versioned_value(offset):
    name = "O'Clock" if offset is not None else None
    value = ExactClock(3723 * 10**12 + 123456789012, 12, offset, name)
    text = normalize_value(value, "VARCHAR")
    document = json.loads(text)

    assert document["v"] == 1
    assert decode_value(document["value"]) == value
    connection = DriverParameterConnection()
    insert.insert_rows(
        get_backend("trino"),
        connection,
        "sandbox.clocks",
        ["clock"],
        [(value,)],
        target_column_types={"clock": "VARCHAR"},
        trino_insert_chunk_size=1,
    )

    statement, parameters, rendered = connection.statements[0]
    assert "CAST(? AS VARCHAR)" in statement
    assert parameters == [text]
    assert literal(value, "VARCHAR") == rendered[0]
    assert json.loads(rendered[0][1:-1].replace("''", "'")) == document
    assert connection.cursor_close_calls == 1


@pytest.mark.parametrize(("offset", "suffix"), [(None, ""), (0, " +00:00"), (330, " +05:30")])
def test_untyped_microsecond_aligned_clock_uses_the_native_sdk_time(offset, suffix):
    value = ExactClock(3723 * 10**12 + 123456000000, 12, offset)
    connection = DriverParameterConnection()

    insert.insert_rows(
        get_backend("trino"),
        connection,
        "sandbox.clocks",
        ["clock"],
        [(value,)],
        trino_insert_chunk_size=1,
    )

    statement, parameters, rendered = connection.statements[0]
    assert "CAST(" not in statement
    assert isinstance(parameters[0], dt.time)
    assert parameters[0].microsecond == 123456
    assert parameters[0].utcoffset() == (None if offset is None else dt.timedelta(minutes=offset))
    assert rendered == [f"TIME '01:02:03.123456{suffix}'"]
    assert connection.cursor_close_calls == 1


@pytest.mark.parametrize("offset", [None, 0])
def test_untyped_picosecond_clock_replay_rejects_before_sdk_submission(offset):
    value = ExactClock(1, 12, offset)
    connection = DriverParameterConnection()
    with ReplaySpool(["clock"]) as spool:
        path = spool.path
        spool.write_rows([(value,)])
        spool.certify()
        rows = list(spool.iter_rows())

        assert rows == [(value,)]
        with pytest.raises(ValueError, match=r"Sub-microsecond.*explicit TIME precision"):
            insert.insert_rows(
                get_backend("trino"),
                connection,
                "sandbox.clocks",
                ["clock"],
                rows,
                trino_insert_chunk_size=1,
            )
        assert connection.statements == []
        assert connection.cursor_close_calls == 1
    assert not path.exists()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            dt.datetime.fromisoformat("2024-01-02T03:04:05.123456"),
            "2024-01-02 03:04:05.123456",
        ),
        (ExactTemporal(123456000000, 12), "1970-01-01 00:00:00.123456"),
        (ExactTemporal(-123456000000, 12, "UTC"), "1969-12-31 23:59:59.876544 UTC"),
    ],
)
def test_untyped_exact_microsecond_timestamp_literals_use_the_ordinary_precision(value, expected):
    assert literal(value, None) == f"TIMESTAMP '{expected}'"
