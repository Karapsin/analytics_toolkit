"""Native Greenplum DATE assignment survives public dataframe certification."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.gp.date_values import GreenplumDateValueError
from analytics_toolkit.sql.backends.gp.native_assignment import NativeAssignmentError

from tests.sql._support.gp_dates import date_target


def _load(frame, *, explicit):
    options = {"table_schema": {"payload": "DATE"}} if explicit else {}
    return sql.load_df(
        "gp",
        "sandbox.events",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        **options,
    )


def _wire_literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, dt.datetime):
        return f"CAST('{value.isoformat()}' AS TIMESTAMP)"
    if isinstance(value, dt.date):
        return f"CAST('{value.isoformat()}' AS DATE)"
    return f"'{value}'"


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "values",
    [
        ["2024-02-29", "2024-03-01", None],
        [
            None,
            dt.datetime.fromisoformat("2024-02-29T23:59:59.123456"),
            dt.datetime.fromisoformat("2024-03-01T00:00:00"),
        ],
        [pd.Timestamp("2024-02-29T23:59:59.123456"), None, pd.Timestamp("2024-03-01")],
        [dt.date(2024, 2, 29), None, dt.date(2024, 3, 1)],
        [
            "2024-02-29",
            dt.datetime.fromisoformat("2024-03-01T23:59:00"),
            dt.date(2024, 3, 2),
            None,
        ],
        [None, None],
    ],
    ids=["iso-strings", "naive-datetime", "naive-timestamp", "dates", "mixed", "all-null"],
)
def test_gp_date_load_keeps_native_assignment_and_real_psycopg_formatter(
    monkeypatch, explicit, values
):
    database = date_target(monkeypatch)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)
    expected = [
        None
        if value is None
        else value.date()
        if isinstance(value, dt.datetime)
        else value
        if isinstance(value, dt.date)
        else dt.date.fromisoformat(value)
        for value in values
    ]

    assert _load(frame, explicit=explicit) == len(values)

    assert database.target.schema == {"payload": "DATE"}
    assert database.target.rows == [{"payload": value} for value in expected]
    bound = [dt.date.fromisoformat(value) if isinstance(value, str) else value for value in values]
    assert database.bound_rows == [(value,) for value in bound]
    assert database.wire_rows == [(_wire_literal(value),) for value in bound]
    assert [kind for kind, _statement in database.submissions] == ["CLEAR", "INSERT"]
    assert [row for _template, row, _wire in database.formatter_observations] == [
        (value,) for value in bound
    ]
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)
    assert database.opened
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("bad", ["2024-02-30", "not-a-date"], ids=["invalid-day", "malformed"])
def test_gp_invalid_late_date_rejects_before_target_clear(monkeypatch, explicit, bad):
    database = date_target(monkeypatch)
    original_target = database.target
    before = deepcopy(database.target.rows)
    frame = pd.DataFrame({"payload": pd.Series(["2024-02-29", bad], dtype=object)})
    original = frame.copy(deep=True)

    expected_error = GreenplumDateValueError if bad == "2024-02-30" else NativeAssignmentError
    with pytest.raises(expected_error) as error:
        _load(frame, explicit=explicit)

    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert bad not in str(error.value)
    assert database.target.native_creation_oid == original_target.native_creation_oid
    assert database.target.rows == before
    assert database.target.schema == {"payload": "DATE"}
    assert database.live_mutations == []
    if bad == "2024-02-30":
        assert database.bound_rows == []
    else:
        # Native syntax is parsed in an owned DATE checkpoint, before any live
        # target action; failed private parameter binding is observable.
        assert database.bound_rows == [(dt.date(2024, 2, 29),), (bad,)]
    assert database.wire_rows == []
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in database.opened)
