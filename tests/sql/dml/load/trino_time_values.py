"""Native datetime.time remains accepted through public Trino dataframe loads."""

from __future__ import annotations

import datetime as dt
import json
from copy import deepcopy
from enum import Enum

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.core.exact_values import decode_value
from sqlglot import exp, parse_one
from zoneinfo import ZoneInfo

from tests.sql._support.load_table import _write_trino_connections
from tests.sql._support.trino_time_values import (
    forbid_parquet_access,
    native_time_target,
    parquet_subset_target,
)


def load(frame, **options):
    return sql.load_df(
        options.pop("db_key", "trino"),
        "iceberg.sandbox.target",
        frame,
        retry_cnt=options.pop("retry_cnt", 1),
        timeout_increment=0,
        trino_insert_chunk_size=2,
        **options,
    )


@pytest.mark.parametrize(
    ("native_type", "value", "wire"),
    [
        ("TIME(6)", dt.time(12, 30, 45, 123456), "'12:30:45.123456'"),
        (
            "TIME(6) WITH TIME ZONE",
            dt.time(12, 30, 45, 123456, tzinfo=dt.timezone(dt.timedelta(hours=3))),
            "'12:30:45.123456+03:00'",
        ),
    ],
)
@pytest.mark.parametrize("explicit_schema", [False, True], ids=["existing", "explicit"])
def test_native_time_uses_real_trino_adapter_and_sdk_formatter(
    monkeypatch, native_type, value, wire, explicit_schema
):
    connection = native_time_target(monkeypatch, native_type)
    frame = pd.DataFrame({"payload": pd.Series([None, value, None], dtype=object)})
    original_frame = frame.copy(deep=True)
    options = {"table_schema": {"payload": native_type}} if explicit_schema else {}

    assert load(frame, write_mode="append", **options) == 3

    assert len(connection.statements) == 2
    assert connection.statements[0][2] == ["NULL", wire]
    assert connection.statements[1][2] == ["NULL"]
    assert f"CAST(? AS {native_type})" in connection.statements[0][0]
    assert all(client.close_calls == 1 for client in connection.opened)
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize("explicit_schema", [False, True], ids=["existing", "explicit"])
def test_enum_clock_and_null_follow_native_time_binding_without_changing_caller(
    monkeypatch, explicit_schema
):
    class ClockCell(Enum):
        NULL = None
        CLOCK = dt.time(12, 30, 45, 123456)

    native = "TIME(6)"
    database = native_time_target(monkeypatch, native)
    incoming = [ClockCell.NULL, ClockCell.CLOCK, ClockCell.NULL]
    frame = pd.DataFrame({"payload": pd.Series(incoming, dtype=object)})
    options = {"table_schema": {"payload": native}} if explicit_schema else {}

    assert load(frame, write_mode="truncate_insert", **options) == 3

    assert database.rows == [None, ClockCell.CLOCK.value, None]
    inserts = [wire for statement, _params, wire in database.statements if "INSERT" in statement]
    assert inserts == [["NULL", "'12:30:45.123456'"], ["NULL"]]
    assert all(frame.iloc[index, 0] is original for index, original in enumerate(incoming))


@pytest.mark.parametrize(
    "timezone",
    [
        dt.timezone.utc,
        dt.timezone(dt.timedelta(0), "custom UTC name"),
        dt.timezone(dt.timedelta(hours=-7, minutes=-30), "custom west"),
        dt.timezone(dt.timedelta(hours=14)),
        dt.timezone(dt.timedelta(hours=-14)),
    ],
)
def test_aware_native_time_captures_offset_independently_of_timezone_name(monkeypatch, timezone):
    native = "TIME(6) WITH TIME ZONE"
    database = native_time_target(monkeypatch, native)
    value = dt.time(12, 30, 45, 123456, tzinfo=timezone)
    frame = pd.DataFrame({"payload": pd.Series([None, value, None], dtype=object)})

    assert load(frame, write_mode="truncate_insert") == 3

    assert database.rows == [None, value, None]
    assert database.rows[1].utcoffset() == value.utcoffset()
    assert database.mutations == ["clear", "insert", "insert"]
    assert database.statements[-2][2][1] == repr(value.isoformat())
    assert value.tzinfo is timezone
    assert frame.iloc[1, 0] is value


@pytest.mark.parametrize("native", ["TIME(3)", "TIME", "TIME(3) WITH TIME ZONE"])
def test_existing_time_precision_rejects_inexact_values_before_clearing_target(monkeypatch, native):
    database = native_time_target(monkeypatch, native)
    original_rows = deepcopy(database.rows)
    timezone = dt.timezone.utc if "WITH TIME ZONE" in native else None
    value = dt.time(12, 30, 45, 123789, tzinfo=timezone)
    frame = pd.DataFrame({"payload": pd.Series([None, value, None], dtype=object)})

    with pytest.raises(ValueError, match=r"TIME|time|precision"):
        load(frame, write_mode="truncate_insert")

    assert database.rows == original_rows
    assert database.mutations == []
    assert database.statements == []


@pytest.mark.parametrize(
    ("native", "value", "expected"),
    [
        ("TIME(3)", dt.time(12, 30, 45, 123789), dt.time(12, 30, 45, 124000)),
        ("TIME", dt.time(12, 30, 45, 123789), dt.time(12, 30, 45, 124000)),
        ("TIME(0)", dt.time(23, 59, 59, 999999), dt.time(0)),
        (
            "TIME(3) WITH TIME ZONE",
            dt.time(23, 59, 59, 999999, tzinfo=dt.timezone.utc),
            dt.time(0, tzinfo=dt.timezone.utc),
        ),
    ],
)
def test_explicit_time_precision_uses_native_rounding_and_midnight_wrap(
    monkeypatch, native, value, expected
):
    database = native_time_target(monkeypatch, native)
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})
    original_frame = frame.copy(deep=True)

    assert load(frame, table_schema={"payload": native}, write_mode="truncate_insert") == 1

    assert database.rows == [expected]
    assert database.mutations == ["clear", "insert"]
    assert f"CAST(? AS {native})" in database.statements[-1][0]
    pd.testing.assert_frame_equal(frame, original_frame)


@pytest.mark.parametrize(
    ("requested", "value", "expected", "wire"),
    [
        (
            "TIME(3)",
            dt.time(12, 30, 45, 123789),
            dt.time(12, 30, 45, 124000),
            "'12:30:45.124000'",
        ),
        ("TIME(0)", dt.time(23, 59, 59, 999999), dt.time(0), "'00:00:00'"),
    ],
)
def test_explicit_precision_conversion_preserves_existing_time6_destination(
    monkeypatch, requested, value, expected, wire
):
    database = native_time_target(monkeypatch, "TIME(6)")
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    assert load(frame, table_schema={"payload": requested}, write_mode="truncate_insert") == 1

    assert database.native_type == "TIME(6)"
    assert database.table_types[database.target] == "TIME(6)"
    assert database.rows == [expected]
    assert database.statements[-1][2] == [wire]
    assert f"CAST(? AS {requested})" in database.statements[-1][0]
    assert frame.iloc[0, 0] is value


@pytest.mark.parametrize(
    "value",
    [
        dt.time(1, tzinfo=dt.timezone(dt.timedelta(hours=14, minutes=1))),
        dt.time(1, tzinfo=dt.timezone(dt.timedelta(hours=-14, minutes=-1))),
        dt.time(1, tzinfo=dt.timezone(dt.timedelta(seconds=30))),
        dt.time(1, tzinfo=dt.timezone(dt.timedelta(microseconds=1))),
        dt.time(1, tzinfo=ZoneInfo("America/New_York")),
        dt.timedelta(hours=1),
        dt.datetime(2024, 1, 1, 1, tzinfo=dt.timezone.utc),
    ],
)
@pytest.mark.parametrize("explicit_schema", [False, True], ids=["existing", "explicit"])
def test_unsupported_time_values_fail_before_target_clear(monkeypatch, value, explicit_schema):
    native = "TIME(6) WITH TIME ZONE"
    database = native_time_target(monkeypatch, native)
    original_rows = deepcopy(database.rows)
    frame = pd.DataFrame({"payload": pd.Series([None, value, None], dtype=object)})
    options = {"table_schema": {"payload": native}} if explicit_schema else {}

    with pytest.raises((TypeError, ValueError), match=r"TIME|time|offset|zone|clock"):
        load(frame, write_mode="truncate_insert", **options)

    assert database.rows == original_rows
    assert database.mutations == []
    assert database.statements == []


@pytest.mark.parametrize(
    "requested",
    ["ARRAY(TIME(13))", "ARRAY(TIME(+3))", "ARRAY(TIME(-0))", "TIME(6)[]", "TUPLE(TIME(6))"],
)
@pytest.mark.parametrize("value", [None, []], ids=["null", "empty-array"])
def test_invalid_nested_time_declaration_rejects_null_and_empty_input_before_clear(
    monkeypatch, requested, value
):
    database = native_time_target(monkeypatch, "ARRAY(TIME(6))")
    database.table_rows[database.target] = [[dt.time(1)], [dt.time(2)]]
    original_rows = deepcopy(database.rows)
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    with pytest.raises(ValueError, match=r"TIME|time|precision|schema|type"):
        load(frame, table_schema={"payload": requested}, write_mode="truncate_insert")

    assert database.rows == original_rows
    assert database.mutations == []
    assert database.statements == []
    assert frame.iloc[0, 0] is value


@pytest.mark.parametrize("native", ["TIME(6)", "TIME(6) WITH TIME ZONE"])
def test_all_null_time_values_remain_sql_null(monkeypatch, native):
    database = native_time_target(monkeypatch, native)
    frame = pd.DataFrame({"payload": pd.Series([None, pd.NA, None], dtype=object)})

    assert load(frame, write_mode="truncate_insert") == 3

    assert database.rows == [None, None, None]
    inserts = [wire for statement, _params, wire in database.statements if "INSERT" in statement]
    assert inserts == [["NULL", "NULL"], ["NULL"]]


def test_aware_time_to_plain_time_retains_native_wallclock_cast(monkeypatch):
    database = native_time_target(monkeypatch, "TIME(6)")
    value = dt.time(12, 30, 45, 123456, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    assert load(frame, write_mode="truncate_insert") == 1

    assert database.rows == [value.replace(tzinfo=None)]
    assert database.statements[-1][2] == ["'12:30:45.123456'"]
    assert "CAST(? AS TIME(6))" in database.statements[-1][0]


def test_naive_time_to_zoned_time_leaves_session_timezone_to_native_cast(monkeypatch):
    native = "TIME(6) WITH TIME ZONE"
    database = native_time_target(monkeypatch, native)
    value = dt.time(12, 30, 45, 123456)
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    assert load(frame, write_mode="append") == 1

    assert database.statements[-1][2] == ["TIME '12:30:45.123456'"]
    bound = database.statements[-1][1][0]
    assert type(bound) is dt.time
    assert bound == value
    assert bound.tzinfo is None
    assert f"CAST(? AS {native})" in database.statements[-1][0]
    assert frame.iloc[0, 0] is value


def test_clock_retry_reuses_captured_offset_after_lost_insert_acknowledgement(monkeypatch):
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
    database = native_time_target(monkeypatch, "TIME(6) WITH TIME ZONE")
    database.lost_insert_acknowledgements = 1
    frame = pd.DataFrame({"payload": pd.Series([clock], dtype=object)})
    assert zone.calls == 0

    assert load(frame, write_mode="append", retry_cnt=2) == 1

    assert zone.calls == 1
    assert database.lost_insert_acknowledgements == 0
    assert database.mutations == ["insert", "insert"]
    wire = [values for _statement, _params, values in database.statements]
    assert wire == [["'12:30:45.123456+03:00'"]] * 2
    assert all(value.utcoffset() == dt.timedelta(hours=3) for value in database.rows[-2:])
    assert frame.iloc[0, 0] is clock
    assert clock.tzinfo is zone


def test_inferred_naive_clock_column_keeps_exact_native_time_and_sql_null(monkeypatch):
    database = native_time_target(monkeypatch, "VARCHAR", exists=False)
    value = dt.time(1, 2, 3, 4)
    frame = pd.DataFrame({"payload": pd.Series([None, value, None], dtype=object)})

    assert load(frame) == 3

    assert database.native_type == "TIME(6)"
    assert database.rows == [None, value, None]
    assert type(database.rows[1]) is dt.time
    inserts = [wire for statement, _params, wire in database.statements if "INSERT" in statement]
    assert inserts == [["NULL", "'01:02:03.000004'"], ["NULL"]]
    assert frame.iloc[1, 0] is value


@pytest.mark.parametrize(
    "value",
    [
        dt.time(1, 2, 3, 4, tzinfo=dt.timezone(dt.timedelta(hours=18))),
        dt.time(1, 2, 3, 4, tzinfo=dt.timezone(dt.timedelta(seconds=30))),
        dt.time(1, 2, 3, 4, tzinfo=ZoneInfo("America/New_York")),
    ],
)
def test_inferred_unsupported_clock_offsets_keep_exact_typed_text(monkeypatch, value):
    database = native_time_target(monkeypatch, "VARCHAR", exists=False)
    frame = pd.DataFrame({"payload": pd.Series([None, value, None], dtype=object)})

    assert load(frame) == 3

    assert database.native_type == "VARCHAR"
    assert database.rows[0] is database.rows[2] is None
    document = json.loads(database.rows[1])
    assert document["v"] == 1
    restored = decode_value(document["value"])
    assert (restored.hour, restored.minute, restored.second, restored.microsecond) == (1, 2, 3, 4)
    assert restored.utcoffset() == value.utcoffset()
    if isinstance(value.tzinfo, ZoneInfo):
        assert restored.tzinfo.key == value.tzinfo.key
    assert frame.iloc[1, 0] is value


def test_schema_none_clock_snapshot_survives_typed_text_write_replay(monkeypatch):
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
    database = native_time_target(monkeypatch, "VARCHAR")
    database.lost_insert_acknowledgements = 1
    frame = pd.DataFrame({"payload": pd.Series([clock], dtype=object)})
    assert zone.calls == 0

    assert load(frame, write_mode="append", retry_cnt=2) == 1

    assert zone.calls == 1
    assert database.mutations == ["insert", "insert"]
    assert database.rows[-1] == database.rows[-2]
    restored = decode_value(json.loads(database.rows[-1])["value"])
    assert restored.utcoffset() == dt.timedelta(hours=3)
    inserts = [wire for statement, _params, wire in database.statements if "INSERT" in statement]
    assert inserts[0] == inserts[1]
    assert frame.iloc[0, 0] is clock
    assert clock.tzinfo is zone


def test_time_dry_run_resolves_in_memory_without_opening_target(monkeypatch):
    database = native_time_target(monkeypatch, "TIME(6)")
    frame = pd.DataFrame({"payload": pd.Series([dt.time(1, 2, 3, 4)], dtype=object)})

    plan = load(frame, table_schema={"payload": "TIME(6)"}, dry_run=True)

    assert plan.operation == "load_df"
    assert database.opened == []
    assert database.statements == []


@pytest.mark.parametrize("explicit_schema", [False, True], ids=["existing", "explicit"])
def test_unsupported_parquet_time_stage_rejects_before_clearing_target(
    monkeypatch, write_sql_connections, explicit_schema
):
    _write_trino_connections(
        write_sql_connections,
        s3_transfer_staging_location="s3://private-test-stage/clock",
    )
    native = "TIME(6)"
    database = native_time_target(monkeypatch, native)
    original_rows = deepcopy(database.rows)
    frame = pd.DataFrame({"payload": pd.Series([dt.time(1, 2, 3, 4)], dtype=object)})
    options = {"table_schema": {"payload": native}} if explicit_schema else {}

    with pytest.raises(ValueError, match=r"TIME|time|Parquet|parquet"):
        load(frame, db_key="trino_stage", write_mode="truncate_insert", **options)

    assert database.rows == original_rows
    assert database.mutations == []
    assert database.statements == []


@pytest.mark.parametrize(
    ("write_mode", "empty_policy", "mutations"),
    [
        ("truncate_insert", "replace", ["clear"]),
        ("replace", "replace", ["drop", "create"]),
        ("replace", "keep", []),
    ],
)
def test_empty_native_time_preserves_clear_replace_keep_without_parquet_stage(
    monkeypatch, write_sql_connections, write_mode, empty_policy, mutations
):
    _write_trino_connections(
        write_sql_connections,
        s3_transfer_staging_location="s3://private-test-stage/clock",
    )
    native = "TIME(6)"
    database = native_time_target(monkeypatch, native)
    original_rows = deepcopy(database.rows)
    parquet_calls = forbid_parquet_access(monkeypatch)
    frame = pd.DataFrame({"payload": pd.Series(dtype=object)})

    assert (
        load(
            frame,
            db_key="trino_stage",
            table_schema={"payload": native},
            write_mode=write_mode,
            empty_source_policy=empty_policy,
        )
        == 0
    )

    if write_mode == "replace" and empty_policy == "replace":
        assert not database.failures, database.failures
        assert database.mutations.count("rename") == 2
        assert database.mutations.count("create_aux") == 2
        assert set(database.table_rows) == {database.target}
    else:
        assert database.mutations == mutations
    assert database.rows == ([] if mutations else original_rows)
    assert database.native_type == native
    assert parquet_calls == []
    inserts = [
        wire
        for statement, _params, wire in database.statements
        if isinstance(parse_one(statement, read="trino"), exp.Insert)
    ]
    assert all(not wire for wire in inserts)


def test_parquet_append_omitting_nullable_time_stages_only_incoming_columns(
    monkeypatch, write_sql_connections
):
    _write_trino_connections(
        write_sql_connections,
        s3_transfer_staging_location="s3://private-test-stage/clock",
    )
    database = parquet_subset_target(monkeypatch)

    assert load(pd.DataFrame({"id": [1, 2]}), db_key="trino_stage", write_mode="append") == 2

    assert database.writes == [(["id"], {"id": "BIGINT"})]
    trees = [parse_one(statement, read="trino") for statement in database.commands]
    creates = [tree for tree in trees if isinstance(tree, exp.Create)]
    assert len(creates) == 1
    assert isinstance(creates[0].this, exp.Schema)
    assert [column.name for column in creates[0].this.expressions] == ["id"]
    inserts = [tree for tree in trees if isinstance(tree, exp.Insert)]
    assert len(inserts) == 1
    assert isinstance(inserts[0].this, exp.Schema)
    assert [column.name for column in inserts[0].this.expressions] == ["id"]
    assert [column.find(exp.Column).name for column in inserts[0].expression.expressions] == ["id"]
    assert all(not tree.find(exp.Star) for tree in inserts)
    assert len(database.cleaned) == 1
