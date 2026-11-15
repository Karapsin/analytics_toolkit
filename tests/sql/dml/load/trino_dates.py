"""Authoritative Trino DATE preserves supported native calendar conversions."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.trino.date_values import TrinoDateValueError
from sqlglot import exp, parse_one

from tests.sql._support.trino_dates import native_date_target

_LEAP = dt.date(2024, 2, 29)
_CASES = [
    ("2024-02-29", _LEAP),
    (pd.Timestamp("2024-02-29"), _LEAP),
    (
        dt.datetime(2024, 3, 1, 0, 30, tzinfo=dt.timezone(dt.timedelta(hours=14))),
        dt.date(2024, 3, 1),
    ),
    (_LEAP, _LEAP),
]


def _load(frame, *, explicit, write_mode="truncate_insert", **options):
    schema = {"table_schema": {"payload": "DATE"}} if explicit else {}
    return sql.load_df(
        "trino",
        "iceberg.sandbox.target",
        frame,
        write_mode=write_mode,
        retry_cnt=1,
        timeout_increment=0,
        trino_insert_chunk_size=2,
        **schema,
        **options,
    )


def _insertions(database):
    return [
        (statement, params, wire)
        for statement, params, wire in database.statements
        if isinstance(parse_one(statement, read="trino"), exp.Insert)
    ]


def _assert_closed(database):
    assert database.opened
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "case", _CASES, ids=["iso-date", "midnight-stamp", "aware-local-day", "date"]
)
def test_trino_dates_public_load_preserves_native_calendar_and_null_values(
    monkeypatch, explicit, case
):
    incoming, expected = case
    database = native_date_target(monkeypatch)
    frame = pd.DataFrame({"payload": pd.Series([None, incoming, None], dtype=object)})
    original = frame.copy(deep=True)

    result = _load(frame, explicit=explicit)

    assert type(result) is int
    assert result == 3
    assert database.rows == [None, expected, None]
    assert database.native_type == "DATE"
    assert database.table_types == {database.target: "DATE"}
    assert database.mutations == ["clear", "insert", "insert"]
    inserts = _insertions(database)
    assert len(inserts) == 2
    assert all("CAST(? AS DATE)" in statement for statement, _params, _wire in inserts)
    params = [value for _statement, values, _wire in inserts for value in values]
    assert params[0] is None
    assert params[2] is None
    wire = [value for _statement, _params, values in inserts for value in values]
    assert wire[0] == wire[2] == "NULL"
    if isinstance(incoming, str):
        assert params[1] in (incoming, expected)
        assert wire[1] in ("'2024-02-29'", "DATE '2024-02-29'")
    else:
        assert type(params[1]) is dt.date
        assert params[1] == expected
        assert wire[1] == f"DATE '{expected.isoformat()}'"
    assert any(
        "information_schema.columns" in statement
        for statement, _params, _wire in database.statements
    )
    _assert_closed(database)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("bad", ["2024-02-30", "not-a-date"], ids=["invalid-day", "invalid-text"])
def test_trino_dates_invalid_last_value_preserves_existing_rows_before_mutation(
    monkeypatch, explicit, bad
):
    database = native_date_target(monkeypatch)
    before = deepcopy(database.table_rows)
    frame = pd.DataFrame({"payload": pd.Series([_LEAP, bad], dtype=object)})
    original = frame.copy(deep=True)

    with pytest.raises(ValueError, match="DATE"):
        _load(frame, explicit=explicit)

    assert database.table_rows == before
    assert database.table_types == {database.target: "DATE"}
    assert database.mutations == []
    assert _insertions(database) == []
    _assert_closed(database)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("hour", [0, 12], ids=["midnight", "nonmidnight"])
def test_trino_dates_equal_native_keys_reject_before_append_stage_or_target_mutation(
    monkeypatch, explicit, hour
):
    database = native_date_target(monkeypatch)
    before = deepcopy(database.table_rows)
    frame = pd.DataFrame(
        {
            "payload": pd.Series(
                ["2024-02-29", pd.Timestamp(f"2024-02-29 {hour:02d}:00:00")],
                dtype=object,
            )
        }
    )
    original = frame.copy(deep=True)
    assert not frame.duplicated(["payload"]).any()

    with pytest.raises(ValueError, match="Duplicate key"):
        _load(frame, explicit=explicit, write_mode="append", key_columns="payload")

    assert database.table_rows == before
    assert database.mutations == []
    assert _insertions(database) == []
    assert database.table_types == {database.target: "DATE"}
    _assert_closed(database)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("values", [[_LEAP], [None], []], ids=["native-date", "null-only", "empty"])
def test_trino_dates_parameterized_date_declaration_rejects_before_any_mutation(
    monkeypatch, values
):
    database = native_date_target(monkeypatch)
    before = deepcopy(database.table_rows)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    with pytest.raises(TrinoDateValueError, match="unparameterized DATE type") as error:
        sql.load_df(
            "trino",
            "iceberg.sandbox.target",
            frame,
            write_mode="truncate_insert",
            empty_source_policy="replace",
            table_schema={"payload": "DATE(3)"},
            retry_cnt=1,
            timeout_increment=0,
        )

    assert type(error.value) is TrinoDateValueError
    assert database.table_rows == before
    assert database.table_types == {database.target: "DATE"}
    assert database.mutations == []
    assert _insertions(database) == []
    _assert_closed(database)
    pd.testing.assert_frame_equal(frame, original)
