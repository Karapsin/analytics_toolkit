"""Supported Greenplum TIME/INTERVAL cells survive public load validation."""

from __future__ import annotations

import datetime as dt
import json
from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.core.exact_values import ExactClock, decode_value
from sqlglot import exp, parse_one

from tests.sql._support.gp_temporal_values import temporal_target


def load(frame, **options):
    return sql.load_df(
        "gp",
        "sandbox.events",
        frame,
        retry_cnt=options.pop("retry_cnt", 1),
        timeout_increment=0,
        **options,
    )


@pytest.mark.parametrize("offset", [None, 0])
def test_exact_clock_aligned_picosecond_source_loads_into_native_gp_time(monkeypatch, offset):
    native = "TIME(6)" + (" WITH TIME ZONE" if offset is not None else "")
    database = temporal_target(monkeypatch, native)
    raw = ExactClock((12 * 3600 + 30 * 60) * 10**12 + 123456000000, 12, offset)
    frame = pd.DataFrame({"payload": pd.Series([None, raw, None], dtype=object)})
    assert load(frame, write_mode="truncate_insert") == 3
    assert database.target.rows == [{"payload": None}, {"payload": raw.time()}, {"payload": None}]
    assert database.target.schema == {"payload": native}
    assert frame.iloc[1, 0] is raw
    assert (raw.scale, raw.offset_minutes) == (12, offset)


def test_exact_clock_single_picosecond_rejects_existing_native_gp_time_before_clear(monkeypatch):
    database = temporal_target(monkeypatch, "TIME(6)")
    seed = deepcopy(database.target.rows)
    raw = ExactClock(1, 12)
    frame = pd.DataFrame({"payload": pd.Series([raw], dtype=object)})
    with pytest.raises(ValueError, match="precision or fields"):
        load(frame, write_mode="truncate_insert")
    assert database.target.rows == seed
    assert not database.live_mutations
    assert frame.iloc[0, 0] is raw
    assert raw.ticks == 1


def test_explicit_gp_clock_precision_rounds_raw_picoseconds_without_mutating_caller(monkeypatch):
    database = temporal_target(monkeypatch, "TIME(6)")
    raw = ExactClock(123456789012, 12)
    frame = pd.DataFrame({"payload": pd.Series([raw], dtype=object)})
    assert load(frame, write_mode="truncate_insert", table_schema={"payload": "TIME(6)"}) == 1
    assert database.target.rows == [{"payload": dt.time(0, 0, 0, 123457)}]
    assert database.target.schema == {"payload": "TIME(6)"}
    assert frame.iloc[0, 0] is raw
    assert (raw.ticks, raw.scale) == (123456789012, 12)


@pytest.mark.parametrize("offset", [None, 0])
def test_inferred_gp_text_preserves_exact_clock_when_native_precision_cannot(monkeypatch, offset):
    database = temporal_target(monkeypatch, "TIME(6)", exists=False)
    raw = ExactClock(1, 12, offset)
    frame = pd.DataFrame({"payload": pd.Series([raw], dtype=object)})
    assert load(frame, write_mode="append") == 1
    assert database.target.schema == {"payload": "TEXT"}
    encoded = json.loads(database.target.rows[0]["payload"])
    assert encoded["v"] == 1
    assert decode_value(encoded["value"]) == raw
    assert frame.iloc[0, 0] is raw


@pytest.mark.parametrize(
    ("native_type", "value"),
    [("TIME", dt.time(12, 30)), ("INTERVAL", dt.timedelta(days=2, seconds=3, microseconds=4))],
)
@pytest.mark.parametrize("explicit_schema", [False, True], ids=["existing", "explicit"])
def test_native_time_and_interval_load_through_real_gp_execute_values(
    monkeypatch, native_type, value, explicit_schema
):
    database = temporal_target(monkeypatch, native_type)
    frame = pd.DataFrame({"payload": pd.Series([None, value, None], dtype=object)})
    original_frame = frame.copy(deep=True)
    options = {"table_schema": {"payload": native_type}} if explicit_schema else {}

    assert load(frame, write_mode="truncate_insert", **options) == 3

    assert database.target.rows == [{"payload": None}, {"payload": value}, {"payload": None}]
    assert database.target.schema == {"payload": native_type}
    assert len(database.wire_rows) == 3
    assert [row[0] is None for row in database.bound_rows] == [True, False, True]
    assert len(database.live_mutations) == 2
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize(
    ("native_type", "values"),
    [
        ("TIME(6)", [dt.time(0, 0, 0, 1), dt.time(23, 59, 59, 999999)]),
        (
            "INTERVAL(6)",
            [
                dt.timedelta(days=2, seconds=3, microseconds=4),
                -dt.timedelta(days=2, seconds=3, microseconds=4),
            ],
        ),
        (
            "TIME WITH TIME ZONE",
            [
                dt.time(12, 30, 45, 123456, tzinfo=dt.timezone(dt.timedelta(hours=3))),
                dt.time(0, 0, 0, 1, tzinfo=dt.timezone(dt.timedelta(hours=-7, minutes=-30))),
            ],
        ),
    ],
)
@pytest.mark.parametrize("explicit_schema", [False, True], ids=["existing", "explicit"])
def test_full_precision_native_temporals_preserve_null_positions_and_exact_values(
    monkeypatch, native_type, values, explicit_schema
):
    database = temporal_target(monkeypatch, native_type)
    incoming = [None, values[0], None, values[1], None]
    frame = pd.DataFrame({"payload": pd.Series(incoming, dtype=object)})
    original_frame = frame.copy(deep=True)
    options = {"table_schema": {"payload": native_type}} if explicit_schema else {}

    assert load(frame, write_mode="truncate_insert", **options) == len(incoming)

    assert database.target.rows == [{"payload": value} for value in incoming]
    assert database.target.schema == {"payload": native_type}
    assert len(database.wire_rows) == len(incoming)
    assert [row[0] is None for row in database.bound_rows] == [True, False, True, False, True]
    if "WITH TIME ZONE" in native_type:
        assert database.target.rows[1]["payload"].utcoffset() == values[0].utcoffset()
        assert database.target.rows[3]["payload"].utcoffset() == values[1].utcoffset()
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize("native_type", ["TIME", "TIME WITH TIME ZONE", "INTERVAL"])
def test_all_null_native_temporal_column_keeps_sql_null_values(monkeypatch, native_type):
    database = temporal_target(monkeypatch, native_type)
    frame = pd.DataFrame({"payload": pd.Series([None, pd.NA, None], dtype=object)})

    assert load(frame, write_mode="truncate_insert") == 3

    assert database.target.rows == [{"payload": None}, {"payload": None}, {"payload": None}]
    assert database.bound_rows == [(None,), (None,), (None,)]


@pytest.mark.parametrize(
    ("native_type", "valid", "invalid"),
    [
        ("TIME", dt.time(12, 30), dt.timedelta(seconds=1)),
        ("INTERVAL", dt.timedelta(seconds=1), dt.time(12, 30)),
        ("TIME", dt.time(12, 30), {"unsupported": 1}),
        ("INTERVAL", dt.timedelta(seconds=1), {"unsupported": 1}),
    ],
)
@pytest.mark.parametrize("explicit_schema", [False, True], ids=["existing", "explicit"])
def test_incompatible_native_temporal_value_rejects_before_clearing_seed_rows(
    monkeypatch, native_type, valid, invalid, explicit_schema
):
    database = temporal_target(monkeypatch, native_type)
    original_rows = deepcopy(database.target.rows)
    frame = pd.DataFrame({"payload": pd.Series([valid, invalid], dtype=object)})
    original_frame = frame.copy(deep=True)
    options = {"table_schema": {"payload": native_type}} if explicit_schema else {}

    with pytest.raises(ValueError, match=r"TIME|INTERVAL|time|interval"):
        load(frame, write_mode="truncate_insert", **options)

    assert database.target.rows == original_rows
    assert database.live_mutations == []
    assert database.bound_rows == []
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize(
    ("native_type", "value"),
    [
        ("TIME(3)", dt.time(12, 30, 45, 123789)),
        ("INTERVAL(3)", dt.timedelta(seconds=1, microseconds=123789)),
        ("INTERVAL", pd.Timedelta(nanoseconds=1)),
    ],
)
def test_existing_temporal_precision_rejects_inexact_values_before_clear(
    monkeypatch, native_type, value
):
    database = temporal_target(monkeypatch, native_type)
    original_rows = deepcopy(database.target.rows)
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    with pytest.raises(ValueError, match=r"TIME|INTERVAL|precision|microsecond"):
        load(frame, write_mode="truncate_insert")

    assert database.target.rows == original_rows
    assert database.live_mutations == []
    assert database.bound_rows == []


@pytest.mark.parametrize(
    ("native_type", "value", "expected"),
    [
        ("TIME(3)", dt.time(12, 30, 45, 123789), dt.time(12, 30, 45, 124000)),
        (
            "INTERVAL(3)",
            dt.timedelta(seconds=1, microseconds=123789),
            dt.timedelta(seconds=1, microseconds=124000),
        ),
    ],
)
def test_explicit_lower_temporal_precision_uses_native_rounding(
    monkeypatch, native_type, value, expected
):
    database = temporal_target(monkeypatch, native_type)
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})
    original_frame = frame.copy(deep=True)

    assert load(frame, table_schema={"payload": native_type}, write_mode="truncate_insert") == 1

    assert database.target.rows == [{"payload": expected}]
    assert len(database.bound_rows) == 1
    assert database.wire_rows
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize(
    ("native_type", "value"),
    [
        ("TIME", dt.time(12, 30, 45, 123456)),
        ("INTERVAL", dt.timedelta(seconds=1, microseconds=123456)),
    ],
)
def test_new_dataframe_destination_infers_a_native_temporal_family(monkeypatch, native_type, value):
    database = temporal_target(monkeypatch, native_type, exists=False)
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    assert load(frame) == 1

    assert database.target.schema["payload"].startswith(native_type)
    assert database.target.rows == [{"payload": value}]
    assert len(database.bound_rows) == len(database.wire_rows) == 1
    assert database.bound_rows[0][0] is not None


def test_inferred_submicrosecond_interval_uses_exact_text_before_publication(monkeypatch):
    database = temporal_target(monkeypatch, "INTERVAL", exists=False)
    value = pd.Timedelta(1, "ns")
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})
    original_frame = frame.copy(deep=True)

    assert load(frame) == 1

    assert database.target.schema == {"payload": "TEXT"}
    encoded = json.loads(database.target.rows[0]["payload"])
    assert encoded["v"] == 1
    restored = decode_value(encoded["value"])
    assert isinstance(restored, pd.Timedelta)
    assert restored.value == value.value == 1
    assert len(database.bound_rows) == 1
    pd.testing.assert_frame_equal(frame, original_frame)


def test_mutable_clock_offset_is_captured_once_before_native_inference_and_retry(monkeypatch):
    class ChangingZone(dt.tzinfo):
        def __init__(self):
            self.calls = 0

        def utcoffset(self, _value):
            self.calls += 1
            return dt.timedelta(hours=3 if self.calls == 1 else 9)

        def dst(self, _value):
            return None

        def tzname(self, _value):
            return "mutable-clock"

    zone = ChangingZone()
    clock = dt.time(12, 30, 45, 123456, tzinfo=zone)
    database = temporal_target(monkeypatch, "TIME WITH TIME ZONE", exists=False)
    database.lost_insert_acknowledgements = 1
    frame = pd.DataFrame({"payload": pd.Series([clock], dtype=object)})
    assert zone.calls == 0

    assert load(frame, write_mode="append", retry_cnt=2) == 1

    assert zone.calls == 1
    native_type = database.target.schema["payload"]
    assert "WITH TIME ZONE" in native_type or native_type.startswith("TIMETZ")
    expected = dt.time(12, 30, 45, 123456, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    assert database.target.rows == [{"payload": expected}]
    assert all(row["payload"].utcoffset() == dt.timedelta(hours=3) for row in database.target.rows)
    assert len(database.bound_rows) == len(database.wire_rows) == 2
    assert database.bound_rows[0] == database.bound_rows[1]
    assert database.wire_rows[0] == database.wire_rows[1]
    assert database.lost_insert_acknowledgements == 0
    mutations = [
        parse_one(database._ddl_schema_prefix(statement), read=database.dialect)
        for statement in database.live_mutations
    ]
    assert [type(mutation) for mutation in mutations] == [
        exp.Create,
        exp.Insert,
        exp.Drop,
        exp.Create,
        exp.Insert,
    ]
    drop_index = database.commands.index(database.live_mutations[2])
    assert database.existence_probes[0][2] is False
    assert any(
        index > drop_index and not exists for index, _name, exists in database.existence_probes
    )
    assert frame.iloc[0, 0] is clock
    assert clock.tzinfo is zone
    assert (clock.hour, clock.minute, clock.second, clock.microsecond) == (12, 30, 45, 123456)


def test_mixed_naive_and_aware_clock_inference_uses_exact_clock_tags_without_zone_invention(
    monkeypatch,
):
    database = temporal_target(monkeypatch, "TIME", exists=False)
    naive = dt.time(12, 30, 45, 123456)
    aware = dt.time(12, 30, 45, 123456, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    frame = pd.DataFrame({"payload": pd.Series([naive, aware], dtype=object)})
    original_frame = frame.copy(deep=True)

    assert load(frame) == 2

    assert database.target.schema == {"payload": "TEXT"}
    documents = [json.loads(row["payload"]) for row in database.target.rows]
    assert all(document["v"] == 1 for document in documents)
    restored = [decode_value(document["value"]) for document in documents]
    assert restored == [naive, aware]
    assert restored[0].utcoffset() is None
    assert restored[1].utcoffset() == dt.timedelta(hours=3)
    pd.testing.assert_frame_equal(frame, original_frame)
