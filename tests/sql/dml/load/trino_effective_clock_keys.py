"""Plain TIME keys compare native wall clocks rather than Python UTC equality."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.trino_effective_clock_keys import clock_key_target


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_plain_trino_clock_keys_keep_python_equal_native_distinct_values(monkeypatch, explicit):
    database = clock_key_target(monkeypatch)
    clocks = [
        dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=1))),
        dt.time(11, tzinfo=dt.timezone.utc),
    ]
    assert clocks[0] == clocks[1]
    frame = pd.DataFrame({"payload": pd.Series(clocks, dtype=object)})
    original = frame.copy(deep=True)
    options = {"table_schema": {"payload": "TIME(6)"}} if explicit else {}

    result = sql.load_df(
        "trino",
        "iceberg.sandbox.target",
        frame,
        write_mode="append",
        key_columns="payload",
        retry_cnt=1,
        timeout_increment=0,
        **options,
    )

    assert result == 2
    assert type(result) is int
    assert database.rows == [dt.time(1), dt.time(2), dt.time(12), dt.time(11)]
    assert set(database.table_rows) == {database.target}
    assert any(
        "CAST(? AS TIME(6))" in statement for statement, _params, _wire in database.statements
    )
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_plain_trino_clock_keys_reject_python_distinct_native_aliases(monkeypatch, explicit):
    database = clock_key_target(monkeypatch)
    before = deepcopy(database.rows)
    clocks = [
        dt.time(12, tzinfo=dt.timezone(dt.timedelta(hours=1))),
        dt.time(12, tzinfo=dt.timezone.utc),
    ]
    assert clocks[0] != clocks[1]
    frame = pd.DataFrame({"payload": pd.Series(clocks, dtype=object)})
    original = frame.copy(deep=True)
    options = {"table_schema": {"payload": "TIME(6)"}} if explicit else {}

    with pytest.raises(ValueError, match=r"Duplicate.*key"):
        sql.load_df(
            "trino",
            "iceberg.sandbox.target",
            frame,
            write_mode="append",
            key_columns="payload",
            retry_cnt=1,
            timeout_increment=0,
            **options,
        )

    assert database.rows == before
    assert database.mutations == []
    assert database.statements == []
    assert set(database.table_rows) == {database.target}
    pd.testing.assert_frame_equal(frame, original)
