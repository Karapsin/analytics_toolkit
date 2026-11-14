"""Typed native clocks bind through both supported Trino formatter contracts."""

from __future__ import annotations

import copy
import datetime as dt

import pytest
from analytics_toolkit.sql.backends import get_backend
from analytics_toolkit.sql.backends.trino import insert
from analytics_toolkit.sql.backends.trino.parameters import literal, normalize_value
from analytics_toolkit.sql.backends.trino.time_values import (
    convert_time_value,
    time_parameter_value,
)
from analytics_toolkit.sql.core.exact_values import ExactClock

from tests.sql._support.trino_binary_buffers import use_actual_formatter
from tests.sql._support.trino_parameters import DriverParameterConnection, render_parameter


def submit(value, native):
    connection = DriverParameterConnection()
    insert.insert_rows(
        get_backend("trino"),
        connection,
        "sandbox.clock_values",
        ["clock"],
        [(value,)],
        target_column_types={"clock": native},
        trino_insert_chunk_size=1,
    )
    return connection


def aware_clock(*, minutes=330, microsecond=123456, fold=1):
    return dt.time(
        12,
        34,
        56,
        microsecond,
        tzinfo=dt.timezone(dt.timedelta(minutes=minutes), "desk"),
        fold=fold,
    )


@pytest.mark.parametrize("sdk", ["minimum-0.320", "installed"])
@pytest.mark.parametrize(
    ("value", "native", "expected"),
    [
        (aware_clock(), "TIME(6) WITH TIME ZONE", "12:34:56.123456+05:30"),
        (
            aware_clock(minutes=0, microsecond=500000, fold=0),
            "TIME(0) WITH TIME ZONE",
            "12:34:57+00:00",
        ),
        (aware_clock(), "TIME(3)", "12:34:56.123000"),
        (ExactClock(1, 12, -840), "TIME(12)", "00:00:00.000000000001"),
        (
            dt.time(1, 2, 3, 123456, fold=1),
            "TIME(6) WITH TIME ZONE",
            "01:02:03.123456",
        ),
        (None, "TIME(12) WITH TIME ZONE", None),
    ],
)
def test_typed_clock_parameters_preserve_wall_offset_rounding_and_server_policy(
    monkeypatch, sdk, value, native, expected
):
    use_actual_formatter(monkeypatch, sdk)
    original = copy.deepcopy(value)
    if isinstance(value, dt.time):
        assert normalize_value(value, None) is value

    connection = submit(value, native)

    statement, parameters, rendered = connection.statements[0]
    assert f"CAST(? AS {native})" in statement
    assert parameters == [expected]
    assert type(parameters[0]) is type(expected)
    assert rendered == [render_parameter(expected)]
    if value is None:
        assert time_parameter_value(value, native) is None
        assert literal(value, native) == "NULL"
    else:
        assert literal(value, native) == f"CAST('{expected}' AS {native})"
        semantic = convert_time_value(value, native)
        if isinstance(value, ExactClock):
            assert semantic.offset_minutes == value.offset_minutes
        else:
            assert semantic.utcoffset() == value.utcoffset()
            assert semantic.fold == value.fold
    assert value == original
    assert connection.cursor_close_calls == 1


@pytest.mark.parametrize("sdk", ["minimum-0.320", "installed"])
@pytest.mark.parametrize(
    "kind", ["array", "row", "map_values", "map_keys", "mixed_awareness", "mixed_precision"]
)
def test_recursive_clock_parameters_use_compatible_native_casts_without_mutating_callers(
    monkeypatch, sdk, kind
):
    use_actual_formatter(monkeypatch, sdk)
    first, second = aware_clock(), aware_clock(minutes=-840, fold=0)
    zero = aware_clock(minutes=0, fold=0)
    native, source, expected = {
        "array": (
            "ARRAY(TIME(6) WITH TIME ZONE)",
            [first, second, None],
            ["12:34:56.123456+05:30", "12:34:56.123456-14:00", None],
        ),
        "row": (
            "ROW(plain TIME(3), zoned TIME(12) WITH TIME ZONE, absent TIME(6) WITH TIME ZONE)",
            (first, ExactClock(2, 12, -840), None),
            ("12:34:56.123000", "00:00:00.000000000002-14:00", None),
        ),
        "map_values": (
            "MAP(VARCHAR, TIME(6) WITH TIME ZONE)",
            {"first": first, "second": second, "null": None},
            {
                "first": "12:34:56.123456+05:30",
                "second": "12:34:56.123456-14:00",
                "null": None,
            },
        ),
        "map_keys": (
            "MAP(TIME(6) WITH TIME ZONE, VARCHAR)",
            {first: "first", zero: None},
            {"12:34:56.123456+05:30": "first", "12:34:56.123456+00:00": None},
        ),
        "mixed_awareness": (
            "ARRAY(TIME(6) WITH TIME ZONE)",
            [first, dt.time(1, 2, 3, 123456), None],
            ["12:34:56.123456+05:30", "01:02:03.123456", None],
        ),
        "mixed_precision": (
            "ARRAY(TIME(12))",
            [dt.time(0, 0, 0, 1, fold=1), ExactClock(1, 12, 330), None],
            ["00:00:00.000001", "00:00:00.000000000001", None],
        ),
    }[kind]
    original = copy.deepcopy(source)

    connection = submit(source, native)

    statement, parameters, rendered = connection.statements[0]
    assert f"CAST(? AS {native})" in statement
    assert parameters == [expected]
    assert rendered == [render_parameter(expected)]
    assert source == original
    assert first.fold == 1
    assert first.utcoffset() == dt.timedelta(minutes=330)
    assert connection.cursor_close_calls == 1
