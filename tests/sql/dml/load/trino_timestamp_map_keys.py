"""Exact timestamp map keys are certified before destructive Trino loads."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.core.exact_values import ExactTemporal, OrderedMapPairs
from analytics_toolkit.sql.dml.load import AmbiguousTableLoadError
from zoneinfo import ZoneInfo

from tests.sql._support.trino_time_map_keys import ClockMapCast, native_clock_map_target
from tests.sql._support.trino_time_values import native_time_target

UTC = dt.timezone.utc
ZONED_TIMESTAMP = "TIMESTAMP(12) WITH TIME ZONE"


def _load(frame, **options):
    return sql.load_df(
        "trino",
        "iceberg.sandbox.target",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        **options,
    )


@pytest.mark.parametrize("nested_row", [False, True], ids=["scalar", "row"])
@pytest.mark.parametrize(
    "case",
    [
        (ExactTemporal(1, 0, "UTC"), ExactTemporal(1000, 3, "UTC")),
        (ExactTemporal(1, 0, "UTC"), ExactTemporal(1000, 3, "Europe/Moscow")),
        (ExactTemporal(-1, 0, "UTC"), ExactTemporal(-1000, 3, "UTC")),
        (ExactTemporal(1, 0, "UTC"), dt.datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC)),
        (ExactTemporal(1, 0, "UTC"), pd.Timestamp("1970-01-01T01:00:01+01:00")),
    ],
    ids=["scales", "named-zone", "negative-epoch", "python-datetime", "offset-timestamp"],
)
def test_trino_timestamp_map_equal_instants_cannot_clear_existing_rows(
    monkeypatch, nested_row, case
):
    key_type = f"ROW(stamp {ZONED_TIMESTAMP}, code BIGINT)" if nested_row else ZONED_TIMESTAMP
    native = f"MAP({key_type}, VARCHAR)"
    database = native_time_target(monkeypatch, native)
    seed = (
        (dt.datetime(2024, 1, 1, tzinfo=UTC), 7)
        if nested_row
        else dt.datetime(2024, 1, 1, tzinfo=UTC)
    )
    original_rows = [{seed: "original"}]
    database.table_rows[database.target] = deepcopy(original_rows)
    first, second = case
    assert first != second
    keys = ((first, 7), (second, 7)) if nested_row else (first, second)
    incoming = dict(zip(keys, ("first", "second")))
    assert len(incoming) == 2
    frame = pd.DataFrame({"payload": pd.Series([incoming], dtype=object)})

    with pytest.raises((ValueError, AmbiguousTableLoadError)) as failure:
        _load(frame)

    assert database.rows == original_rows
    assert isinstance(failure.value, ValueError)
    assert database.mutations == []
    assert database.statements == []
    assert all(connection.close_calls == 1 for connection in database.opened)
    assert frame.iloc[0, 0] is incoming
    assert list(incoming.keys()) == list(keys)


@pytest.mark.parametrize("nested_row", [False, True], ids=["scalar", "row"])
@pytest.mark.parametrize("awareness", [False, True], ids=["naive", "aware"])
def test_trino_timestamp_map_nanosecond_distinctions_reach_native_p12(
    monkeypatch, nested_row, awareness
):
    zone = "UTC" if awareness else None
    stamp_type = ZONED_TIMESTAMP if awareness else "TIMESTAMP(12)"
    key_type = f"ROW(stamp {stamp_type}, code BIGINT)" if nested_row else stamp_type
    native = f"MAP({key_type}, VARCHAR)"
    first = ExactTemporal(1_000_000_000, 9, zone)
    second = ExactTemporal(1_000_000_001, 9, zone)
    keys = ((first, 7), (second, 7)) if nested_row else (first, second)
    incoming = dict(zip(keys, ("first", "second")))
    suffix = " UTC" if awareness else ""
    wire_keys = (
        f"'1970-01-01 00:00:01.000000000000{suffix}','1970-01-01 00:00:01.000000001000{suffix}'"
    )
    if nested_row:
        wire_keys = (
            f"ROW('1970-01-01 00:00:01.000000000000{suffix}',7),"
            f"ROW('1970-01-01 00:00:01.000000001000{suffix}',7)"
        )
    database = native_clock_map_target(
        monkeypatch,
        native,
        original_rows=[{keys[0]: "original"}],
        expected_cast=ClockMapCast(f"MAP(ARRAY[{wire_keys}], ARRAY['first','second'])", incoming),
    )
    frame = pd.DataFrame({"payload": pd.Series([incoming], dtype=object)})

    assert _load(frame) == 1

    assert database.rows == [incoming]
    assert len(database.rows[0]) == 2
    assert database.mutations == ["clear", "insert"]
    assert database.table_types[database.target] == native
    assert len(database.map_casts) == 1
    assert frame.iloc[0, 0] is incoming
    assert all(original is current for original, current in zip(keys, incoming))


def test_trino_timestamp_map_microsecond_distinctions_reach_supported_native_p6(monkeypatch):
    native = "MAP(TIMESTAMP(6) WITH TIME ZONE, VARCHAR)"
    first = ExactTemporal(1_000_001, 6, "UTC")
    second = ExactTemporal(1_000_002, 6, "UTC")
    incoming = {first: "first", second: "second"}
    expected = {
        dt.datetime(1970, 1, 1, 0, 0, 1, 1, tzinfo=UTC): "first",
        dt.datetime(1970, 1, 1, 0, 0, 1, 2, tzinfo=UTC): "second",
    }
    database = native_clock_map_target(
        monkeypatch,
        native,
        original_rows=[{next(iter(expected)): "original"}],
        expected_cast=ClockMapCast(
            "MAP(ARRAY[TIMESTAMP '1970-01-01 00:00:01.000001 UTC',"
            "TIMESTAMP '1970-01-01 00:00:01.000002 UTC'], ARRAY['first','second'])",
            expected,
        ),
    )
    frame = pd.DataFrame({"payload": pd.Series([incoming], dtype=object)})

    assert _load(frame) == 1

    assert database.rows == [expected]
    assert len(database.rows[0]) == 2
    assert database.mutations == ["clear", "insert"]
    assert database.table_types[database.target] == native
    assert frame.iloc[0, 0] is incoming
    assert all(original is current for original, current in zip((first, second), incoming))


@pytest.mark.parametrize("nested_row", [False, True], ids=["scalar", "row"])
def test_trino_timestamp_map_dst_fold_keeps_distinct_native_instants(monkeypatch, nested_row):
    zone = ZoneInfo("America/New_York")
    first = dt.datetime(2024, 11, 3, 1, 30, tzinfo=zone, fold=0)
    second = dt.datetime(2024, 11, 3, 1, 30, tzinfo=zone, fold=1)
    assert first == second
    keys = ((first, 7), (second, 7)) if nested_row else (first, second)
    incoming = OrderedMapPairs(tuple(zip(keys, ("first", "second"))))
    expected_stamps = (
        dt.datetime(2024, 11, 3, 5, 30, tzinfo=UTC),
        dt.datetime(2024, 11, 3, 6, 30, tzinfo=UTC),
    )
    expected_keys = (
        tuple((stamp, 7) for stamp in expected_stamps) if nested_row else expected_stamps
    )
    expected = dict(zip(expected_keys, ("first", "second")))
    key_type = f"ROW(stamp {ZONED_TIMESTAMP}, code BIGINT)" if nested_row else ZONED_TIMESTAMP
    native = f"MAP({key_type}, VARCHAR)"
    wire_keys = (
        "ROW('2024-11-03 05:30:00.000000000000 UTC',7),"
        "ROW('2024-11-03 06:30:00.000000000000 UTC',7)"
        if nested_row
        else "'2024-11-03 05:30:00.000000000000 UTC','2024-11-03 06:30:00.000000000000 UTC'"
    )
    database = native_clock_map_target(
        monkeypatch,
        native,
        original_rows=[{expected_keys[0]: "original"}],
        expected_cast=ClockMapCast(f"MAP(ARRAY[{wire_keys}], ARRAY['first','second'])", expected),
    )
    frame = pd.DataFrame({"payload": pd.Series([incoming], dtype=object)})

    assert _load(frame) == 1

    assert database.rows == [expected]
    assert len(database.rows[0]) == 2
    assert database.mutations == ["clear", "insert"]
    assert frame.iloc[0, 0] is incoming
    assert incoming[0][0] is keys[0]
    assert incoming[1][0] is keys[1]
    assert first.tzinfo is zone
    assert first.fold == 0
    assert second.tzinfo is zone
    assert second.fold == 1


@pytest.mark.parametrize("nested_row", [False, True], ids=["scalar", "row"])
def test_trino_timestamp_map_explicit_precision_collision_rejects_before_clear(
    monkeypatch, nested_row
):
    first = ExactTemporal(1_000_000_001, 9, "UTC")
    second = ExactTemporal(1_000_000_002, 9, "UTC")
    keys = ((first, 7), (second, 7)) if nested_row else (first, second)
    existing_key = f"ROW(stamp {ZONED_TIMESTAMP}, code BIGINT)" if nested_row else ZONED_TIMESTAMP
    requested_key = (
        "ROW(stamp TIMESTAMP(6) WITH TIME ZONE, code BIGINT)"
        if nested_row
        else "TIMESTAMP(6) WITH TIME ZONE"
    )
    native = f"MAP({existing_key}, VARCHAR)"
    database = native_time_target(monkeypatch, native)
    original_rows = [{keys[0]: "original"}]
    database.table_rows[database.target] = deepcopy(original_rows)
    incoming = dict(zip(keys, ("first", "second")))

    with pytest.raises(ValueError, match=r"map|unique|distinct|key"):
        _load(
            pd.DataFrame({"payload": pd.Series([incoming], dtype=object)}),
            table_schema={"payload": f"MAP({requested_key}, VARCHAR)"},
        )

    assert database.rows == original_rows
    assert database.mutations == []
    assert database.statements == []
    assert len(incoming) == 2


@pytest.mark.parametrize("nested_row", [False, True], ids=["scalar", "row"])
def test_trino_timestamp_map_existing_precision_rejects_inexact_keys_before_clear(
    monkeypatch, nested_row
):
    stamp = ExactTemporal(1_000_000_001, 9, "UTC")
    key = (stamp, 7) if nested_row else stamp
    native_key = (
        "ROW(stamp TIMESTAMP(6) WITH TIME ZONE, code BIGINT)"
        if nested_row
        else "TIMESTAMP(6) WITH TIME ZONE"
    )
    database = native_time_target(monkeypatch, f"MAP({native_key}, VARCHAR)")
    original_rows = [{key: "original"}]
    database.table_rows[database.target] = deepcopy(original_rows)
    incoming = {key: "inexact"}

    with pytest.raises(ValueError, match=r"precision|timestamp|Timestamp"):
        _load(pd.DataFrame({"payload": pd.Series([incoming], dtype=object)}))

    assert database.rows == original_rows
    assert database.mutations == []
    assert database.statements == []


@pytest.mark.parametrize("awareness", [False, True], ids=["naive", "aware"])
def test_trino_timestamp_map_bare_cast_rounding_collision_rejects_before_clear(
    monkeypatch, awareness
):
    zone = UTC if awareness else None
    existing_key = "TIMESTAMP(6) WITH TIME ZONE" if awareness else "TIMESTAMP(6)"
    requested_key = "TIMESTAMP WITH TIME ZONE" if awareness else "TIMESTAMP"
    native = f"MAP({existing_key}, VARCHAR)"
    requested = f"MAP({requested_key}, VARCHAR)"
    first = dt.datetime(1970, 1, 1, 0, 0, 1, 123600, tzinfo=zone)
    second = dt.datetime(1970, 1, 1, 0, 0, 1, 124400, tzinfo=zone)
    incoming = {first: "first", second: "second"}
    suffix = " UTC" if awareness else ""
    original_rows = [{first: "original"}]
    database = native_clock_map_target(
        monkeypatch,
        native,
        original_rows=original_rows,
        expected_cast=ClockMapCast(
            f"MAP(ARRAY[TIMESTAMP '1970-01-01 00:00:01.123600{suffix}',"
            f"TIMESTAMP '1970-01-01 00:00:01.124400{suffix}'], ARRAY['first','second'])",
            native_type=requested,
        ),
    )

    with pytest.raises((ValueError, AmbiguousTableLoadError)) as failure:
        _load(
            pd.DataFrame({"payload": pd.Series([incoming], dtype=object)}),
            table_schema={"payload": requested},
        )

    assert database.rows == original_rows
    assert isinstance(failure.value, ValueError)
    assert database.mutations == []
    assert database.statements == []


@pytest.mark.parametrize("awareness", [False, True], ids=["naive", "aware"])
def test_trino_timestamp_map_bare_cast_preserves_distinct_rounded_keys(monkeypatch, awareness):
    zone = UTC if awareness else None
    existing_key = "TIMESTAMP(6) WITH TIME ZONE" if awareness else "TIMESTAMP(6)"
    requested_key = "TIMESTAMP WITH TIME ZONE" if awareness else "TIMESTAMP"
    native = f"MAP({existing_key}, VARCHAR)"
    requested = f"MAP({requested_key}, VARCHAR)"
    first = dt.datetime(1970, 1, 1, 0, 0, 1, 123600, tzinfo=zone)
    second = dt.datetime(1970, 1, 1, 0, 0, 1, 125400, tzinfo=zone)
    incoming = {first: "first", second: "second"}
    expected = {
        dt.datetime(1970, 1, 1, 0, 0, 1, 124000, tzinfo=zone): "first",
        dt.datetime(1970, 1, 1, 0, 0, 1, 125000, tzinfo=zone): "second",
    }
    suffix = " UTC" if awareness else ""
    database = native_clock_map_target(
        monkeypatch,
        native,
        original_rows=[{first: "original"}],
        expected_cast=ClockMapCast(
            f"MAP(ARRAY[TIMESTAMP '1970-01-01 00:00:01.124000{suffix}',"
            f"TIMESTAMP '1970-01-01 00:00:01.125000{suffix}'], ARRAY['first','second'])",
            expected,
            requested,
        ),
    )
    frame = pd.DataFrame({"payload": pd.Series([incoming], dtype=object)})

    assert _load(frame, table_schema={"payload": requested}) == 1

    assert database.rows == [expected]
    assert len(database.rows[0]) == 2
    assert database.table_types[database.target] == native
    assert database.mutations == ["clear", "insert"]
    assert frame.iloc[0, 0] is incoming
    assert all(original is current for original, current in zip((first, second), incoming))
