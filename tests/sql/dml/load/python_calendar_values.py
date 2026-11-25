"""Native Python timestamp inputs survive minimum-pandas checkpoint limits."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.gp_timestamp_precision import timestamp_precision_target
from tests.sql._support.pandas_calendar_boundary import restrict_exact_timestamp_to_nanoseconds


@pytest.mark.parametrize("year", [1500, 2299])
@pytest.mark.parametrize("explicit", [False, True])
def test_public_gp_load_keeps_python_calendar_values_without_pandas_ns_conversion(
    monkeypatch, year, explicit
):
    restrict_exact_timestamp_to_nanoseconds(monkeypatch)
    database = timestamp_precision_target(monkeypatch, "TIMESTAMP(6)")
    value = dt.datetime.fromisoformat(f"{year:04d}-06-07T12:34:56.123456")
    frame = pd.DataFrame({"payload": pd.Series([value, None], dtype=object)})
    original = frame.copy(deep=True)

    assert (
        sql.load_df(
            "gp",
            "sandbox.events",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
            **({"table_schema": {"payload": "TIMESTAMP(6)"}} if explicit else {}),
        )
        == 2
    )

    assert database.rows == [{"payload": value}, {"payload": None}]
    assert database.schema == {"payload": "TIMESTAMP(6)"}
    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in database.opened)
