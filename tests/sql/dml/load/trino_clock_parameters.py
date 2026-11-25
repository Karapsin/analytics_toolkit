"""Clock casts retain wall time and offsets on every supported Trino formatter."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.core.exact_values import ExactClock
from analytics_toolkit.sql.dml.load import AmbiguousTableLoadError

from tests.sql._support.trino_binary_buffers import use_actual_formatter
from tests.sql._support.trino_time_values import native_time_target


def _load(frame, **options):
    return sql.load_df(
        "trino",
        "iceberg.sandbox.target",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        trino_insert_chunk_size=2,
        **options,
    )


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
@pytest.mark.parametrize("zoned", [False, True], ids=["plain-wall", "fixed-offset"])
@pytest.mark.parametrize("offset", [0, 180, -450], ids=["utc", "east", "west"])
def test_trino_clock_parameters_aware_time_keeps_native_cast_semantics(
    monkeypatch, sdk, zoned, offset
):
    use_actual_formatter(monkeypatch, sdk)
    native = "TIME(6)" + (" WITH TIME ZONE" if zoned else "")
    database = native_time_target(monkeypatch, native)
    original_rows = [dt.time(1, tzinfo=dt.timezone.utc if zoned else None)]
    database.table_rows[database.target] = deepcopy(original_rows)
    timezone = dt.timezone(dt.timedelta(minutes=offset))
    value = dt.time(12, 30, 45, 123456, tzinfo=timezone)
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    try:
        count = _load(frame)
    except AmbiguousTableLoadError:
        assert database.rows == original_rows
        raise

    assert count == 1
    assert database.rows == [value if zoned else value.replace(tzinfo=None)]
    assert database.mutations == ["clear", "insert"]
    assert f"CAST(? AS {native})" in database.statements[-1][0]
    wire = database.statements[-1][2][0]
    if zoned:
        assert wire == repr(value.isoformat(timespec="microseconds"))
        assert database.rows[0].utcoffset() == dt.timedelta(minutes=offset)
    else:
        # Native TIMETZ->TIME casts preserve wall fields; they do not shift to UTC.
        assert wire == "'12:30:45.123456'"
        assert database.rows[0].tzinfo is None
    assert frame.iloc[0, 0] is value
    assert value.tzinfo is timezone
    assert value.hour == 12
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
def test_trino_clock_parameters_naive_time_and_nulls_keep_native_policy(monkeypatch, sdk):
    use_actual_formatter(monkeypatch, sdk)
    database = native_time_target(monkeypatch, "TIME(6)")
    value = dt.time(12, 30, 45, 123456)
    frame = pd.DataFrame({"payload": pd.Series([None, value, None], dtype=object)})

    assert _load(frame) == 3

    assert database.rows == [None, value, None]
    assert database.mutations == ["clear", "insert", "insert"]
    assert database.statements[-2][2] == ["NULL", "'12:30:45.123456'"]
    assert database.statements[-1][2] == ["NULL"]
    assert frame.iloc[1, 0] is value
    assert value.tzinfo is None


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
@pytest.mark.parametrize(
    "case",
    [
        (
            "ARRAY(TIME(6) WITH TIME ZONE)",
            [dt.time(12, 30, 45, 123456, tzinfo=dt.timezone(dt.timedelta(hours=3))), None],
            "ARRAY['12:30:45.123456+03:00',NULL]",
            [dt.time(12, 30, 45, 123456, tzinfo=dt.timezone(dt.timedelta(hours=3))), None],
            [dt.time(1, tzinfo=dt.timezone.utc)],
        ),
        (
            "ROW(plain_clock TIME(6), zoned_clock TIME(6) WITH TIME ZONE)",
            (
                dt.time(12, 30, 45, 123456, tzinfo=dt.timezone(dt.timedelta(hours=3))),
                dt.time(12, 30, 45, 123456, tzinfo=dt.timezone(dt.timedelta(hours=3))),
            ),
            "ROW('12:30:45.123456','12:30:45.123456+03:00')",
            (
                dt.time(12, 30, 45, 123456),
                dt.time(12, 30, 45, 123456, tzinfo=dt.timezone(dt.timedelta(hours=3))),
            ),
            (dt.time(1), dt.time(1, tzinfo=dt.timezone.utc)),
        ),
        (
            "MAP(VARCHAR, TIME(6) WITH TIME ZONE)",
            {
                "known": dt.time(12, 30, 45, 123456, tzinfo=dt.timezone(dt.timedelta(hours=3))),
                "missing": None,
            },
            "MAP(ARRAY['known','missing'], ARRAY['12:30:45.123456+03:00',NULL])",
            {
                "known": dt.time(12, 30, 45, 123456, tzinfo=dt.timezone(dt.timedelta(hours=3))),
                "missing": None,
            },
            {"seed": dt.time(1, tzinfo=dt.timezone.utc)},
        ),
    ],
    ids=["array", "mixed-row", "map-value"],
)
def test_trino_clock_parameters_nested_casts_keep_offsets_and_plain_wall_fields(
    monkeypatch, sdk, case
):
    native, value, wire, expected, original = case
    use_actual_formatter(monkeypatch, sdk)
    database = native_time_target(monkeypatch, native)
    database.table_rows[database.target] = [original]

    def decode(actual):
        if actual == "NULL":
            return None
        assert actual == wire
        return expected

    monkeypatch.setattr(database, "decode_cast", decode)
    frame = pd.DataFrame({"payload": pd.Series([None, value, None], dtype=object)})
    original_input = deepcopy(value)

    assert _load(frame, table_schema={"payload": native}) == 3

    assert database.rows == [None, expected, None]
    assert database.mutations == ["clear", "insert", "insert"]
    assert database.statements[-2][2] == ["NULL", wire]
    assert database.statements[-1][2] == ["NULL"]
    assert f"CAST(? AS {native})" in database.statements[-2][0]
    assert frame.iloc[1, 0] is value
    assert value == original_input
    assert database.native_type == native


@pytest.mark.parametrize("sdk", ["current", "minimum-0.320"])
def test_trino_clock_parameters_mixed_array_has_one_native_cast_input_family(monkeypatch, sdk):
    native = "ARRAY(TIME(12))"
    use_actual_formatter(monkeypatch, sdk)
    database = native_time_target(monkeypatch, native)
    database.table_rows[database.target] = [[dt.time(1)]]
    microseconds = dt.time(12, 30, 45, 123456)
    picoseconds = ExactClock(45_045_123_456_789_001, 12)
    value = [microseconds, picoseconds, None]
    wire = "ARRAY['12:30:45.123456','12:30:45.123456789001',NULL]"

    def decode(actual):
        assert actual == wire
        return [microseconds, picoseconds, None]

    monkeypatch.setattr(database, "decode_cast", decode)
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    assert _load(frame, table_schema={"payload": native}) == 1

    assert database.rows == [value]
    assert database.rows[0][0] == dt.time(12, 30, 45, 123456)
    assert database.rows[0][1].ticks == 45_045_123_456_789_001
    assert database.rows[0][1].scale == 12
    assert database.rows[0][1].offset_minutes is None
    assert database.rows[0][2] is None
    assert f"CAST(? AS {native})" in database.statements[-1][0]
    assert database.statements[-1][2] == [wire]
    assert database.native_type == native
    assert database.mutations == ["clear", "insert"]
    assert frame.iloc[0, 0] is value
