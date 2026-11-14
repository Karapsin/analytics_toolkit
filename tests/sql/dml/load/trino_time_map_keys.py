"""Destination clock-map uniqueness is certified before destructive Trino loads."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.core.exact_values import OrderedMapPairs
from analytics_toolkit.sql.dml.load import AmbiguousTableLoadError

from tests.sql._support.trino_time_map_keys import ClockMapCast, native_clock_map_target

UTC = dt.timezone.utc
EAST_ONE = dt.timezone(dt.timedelta(hours=1))
EAST_TWO = dt.timezone(dt.timedelta(hours=2))


def _load(value, **options):
    return sql.load_df(
        "trino",
        "iceberg.sandbox.target",
        pd.DataFrame({"payload": pd.Series([value], dtype=object)}),
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        **options,
    )


@pytest.mark.parametrize("explicit_schema", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "case",
    [
        (
            "TIME(6)",
            (dt.time(12, tzinfo=UTC), dt.time(12, tzinfo=EAST_ONE)),
            "TIME '12:00:00.000000 +00:00',TIME '12:00:00.000000 +01:00'",
            dt.time(1),
        ),
        (
            "ROW(clock TIME(6), code BIGINT)",
            ((dt.time(12, tzinfo=UTC), 7), (dt.time(12, tzinfo=EAST_ONE), 7)),
            "ROW(TIME '12:00:00.000000 +00:00',7),ROW(TIME '12:00:00.000000 +01:00',7)",
            (dt.time(1), 7),
        ),
        (
            "TIME(6) WITH TIME ZONE",
            (dt.time(23, tzinfo=UTC), dt.time(1, tzinfo=EAST_TWO)),
            "TIME '23:00:00.000000 +00:00',TIME '01:00:00.000000 +02:00'",
            dt.time(1, tzinfo=UTC),
        ),
    ],
    ids=["wall-clock", "row-wall-clock", "zoned-midnight-wrap"],
)
def test_trino_time_map_cast_collision_cannot_clear_existing_rows(
    monkeypatch, explicit_schema, case
):
    key_type, keys, wire_keys, seed_key = case
    native = f"MAP({key_type}, VARCHAR)"
    original_rows = [{seed_key: "original"}]
    database = native_clock_map_target(
        monkeypatch,
        native,
        original_rows=original_rows,
        expected_cast=ClockMapCast(f"MAP(ARRAY[{wire_keys}], ARRAY['first','second'])"),
    )
    incoming = dict(zip(keys, ("first", "second")))
    assert len(incoming) == 2
    options = {"table_schema": {"payload": native}} if explicit_schema else {}

    with pytest.raises((ValueError, AmbiguousTableLoadError)) as failure:
        _load(incoming, **options)

    assert database.rows == original_rows
    assert isinstance(failure.value, ValueError)
    assert database.mutations == []
    assert database.statements == []
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("nested_row", [False, True], ids=["clock", "row-clock"])
def test_trino_time_map_rounding_keeps_distinct_native_wall_clocks(monkeypatch, nested_row):
    first = dt.time(12, 0, 0, 123400, tzinfo=UTC)
    second = dt.time(13, 0, 0, 123499, tzinfo=EAST_ONE)
    raw_keys = ((first, 7), (second, 7)) if nested_row else (first, second)
    incoming = dict(zip(raw_keys, ("first", "second")))
    assert len(incoming) == 2
    expected_keys = (dt.time(12, 0, 0, 123000), dt.time(13, 0, 0, 123000))
    if nested_row:
        expected_keys = tuple((clock, 7) for clock in expected_keys)
    expected = dict(zip(expected_keys, ("first", "second")))
    requested_key = "ROW(clock TIME(3), code BIGINT)" if nested_row else "TIME(3)"
    existing_key = "ROW(clock TIME(6), code BIGINT)" if nested_row else "TIME(6)"
    expected_wire_keys = (
        "ROW('12:00:00.123000',7),ROW('13:00:00.123000',7)"
        if nested_row
        else "'12:00:00.123000','13:00:00.123000'"
    )
    database = native_clock_map_target(
        monkeypatch,
        f"MAP({existing_key}, VARCHAR)",
        original_rows=[{expected_keys[0]: "original"}],
        expected_cast=ClockMapCast(
            f"MAP(ARRAY[{expected_wire_keys}], ARRAY['first','second'])",
            expected,
            f"MAP({requested_key}, VARCHAR)",
        ),
    )
    original_input = deepcopy(incoming)

    assert _load(incoming, table_schema={"payload": f"MAP({requested_key}, VARCHAR)"}) == 1

    assert database.rows == [expected]
    assert len(database.rows[0]) == 2
    assert database.mutations == ["clear", "insert"]
    assert len(database.map_casts) == 1
    assert incoming == original_input
    assert database.native_type == f"MAP({existing_key}, VARCHAR)"


def test_trino_time_map_native_distinct_pairs_do_not_collapse_before_binding(monkeypatch):
    first = dt.time(12, tzinfo=EAST_ONE)
    second = dt.time(11, tzinfo=UTC)
    assert first == second
    incoming = OrderedMapPairs(((first, "first"), (second, "second")))
    expected = {dt.time(12): "first", dt.time(11): "second"}
    database = native_clock_map_target(
        monkeypatch,
        "MAP(TIME(6), VARCHAR)",
        original_rows=[{dt.time(1): "original"}],
        expected_cast=ClockMapCast(
            "MAP(ARRAY['12:00:00','11:00:00'], ARRAY['first','second'])",
            expected,
        ),
    )

    assert _load(incoming) == 1

    assert database.rows == [expected]
    assert len(database.rows[0]) == 2
    assert database.mutations == ["clear", "insert"]
    assert len(incoming) == 2


def test_trino_time_map_zoned_distinct_instants_retain_both_offset_keys(monkeypatch):
    incoming = {
        dt.time(12, tzinfo=UTC): "first",
        dt.time(12, tzinfo=EAST_ONE): "second",
    }
    database = native_clock_map_target(
        monkeypatch,
        "MAP(TIME(6) WITH TIME ZONE, VARCHAR)",
        original_rows=[{dt.time(1, tzinfo=UTC): "original"}],
        expected_cast=ClockMapCast(
            "MAP(ARRAY['12:00:00+00:00','12:00:00+01:00'], ARRAY['first','second'])",
            incoming,
        ),
    )

    assert _load(incoming) == 1

    assert database.rows == [incoming]
    assert len(database.rows[0]) == 2
    assert [key.utcoffset() for key in database.rows[0]] == [dt.timedelta(0), dt.timedelta(hours=1)]
    assert database.mutations == ["clear", "insert"]


@pytest.mark.parametrize(
    ("key_type", "keys"),
    [
        ("TIME(6) WITH TIME ZONE", (dt.time(12), dt.time(12, tzinfo=UTC))),
        ("ARRAY(TIME(6))", ((dt.time(12, tzinfo=UTC),), (dt.time(13, tzinfo=UTC),))),
    ],
    ids=["uncertifiable-session-zone", "unsupported-array-carrier"],
)
def test_trino_time_map_uncertifiable_keys_reject_before_mutation(monkeypatch, key_type, keys):
    native = f"MAP({key_type}, VARCHAR)"
    original_rows = [{keys[0]: "original"}]
    database = native_clock_map_target(monkeypatch, native, original_rows=original_rows)
    incoming = OrderedMapPairs(tuple(zip(keys, ("first", "second"))))

    with pytest.raises(ValueError, match=r"map|clock|TIME|time|offset|hashable|unique"):
        _load(incoming)

    assert database.rows == original_rows
    assert database.statements == []
    assert database.mutations == []
    assert database.map_casts == []


@pytest.mark.parametrize("nested_row", [False, True], ids=["clock", "row-clock"])
def test_trino_time_map_existing_precision_is_checked_before_key_canonicalization(
    monkeypatch, nested_row
):
    clock = dt.time(12, 0, 0, 123499, tzinfo=EAST_ONE)
    key = (clock, 7) if nested_row else clock
    native_key = "ROW(clock TIME(3), code BIGINT)" if nested_row else "TIME(3)"
    seed_key = (dt.time(1), 7) if nested_row else dt.time(1)
    original_rows = [{seed_key: "original"}]
    database = native_clock_map_target(
        monkeypatch, f"MAP({native_key}, VARCHAR)", original_rows=original_rows
    )

    with pytest.raises(ValueError, match=r"precision|TIME|time"):
        _load({key: "inexact"})

    assert database.rows == original_rows
    assert database.statements == []
    assert database.mutations == []
    assert database.map_casts == []


@pytest.mark.parametrize("nested_row", [False, True], ids=["clock", "row-clock"])
def test_trino_time_map_explicit_rounding_collision_rejects_before_clear(monkeypatch, nested_row):
    first = dt.time(12, 0, 0, 123400, tzinfo=UTC)
    second = dt.time(12, 0, 0, 123499, tzinfo=EAST_ONE)
    keys = ((first, 7), (second, 7)) if nested_row else (first, second)
    native_key = "ROW(clock TIME(3), code BIGINT)" if nested_row else "TIME(3)"
    native = f"MAP({native_key}, VARCHAR)"
    seed_key = (dt.time(1), 7) if nested_row else dt.time(1)
    original_rows = [{seed_key: "original"}]
    database = native_clock_map_target(monkeypatch, native, original_rows=original_rows)
    incoming = dict(zip(keys, ("first", "second")))
    assert len(incoming) == 2

    with pytest.raises(ValueError, match=r"map|clock|TIME|time|unique"):
        _load(incoming, table_schema={"payload": native})

    assert database.rows == original_rows
    assert database.statements == []
    assert database.mutations == []
    assert database.map_casts == []
