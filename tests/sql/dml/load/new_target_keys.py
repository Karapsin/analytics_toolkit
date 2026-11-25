"""A new keyed target certifies converted effective keys before creation."""

from __future__ import annotations

import ipaddress

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.gp_cidr_casts import native_cidr_target
from tests.sql._support.gp_effective_keys import effective_key_target


@pytest.mark.parametrize(
    ("native", "values", "converted"),
    [("BIGINT", ["01", "1"], [1, 1]), ("BOOLEAN", ["false", "f"], [False, False])],
)
def test_new_gp_upsert_rejects_converted_key_aliases_before_target_creation(
    monkeypatch, native, values, converted
):
    database = effective_key_target(monkeypatch, native)
    database.tables.clear()
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)
    assert not frame.duplicated().any()

    with pytest.raises(ValueError, match=r"Duplicate.*key"):
        sql.load_df(
            "gp",
            "sandbox.events",
            frame,
            table_schema={"payload": native},
            write_mode="upsert",
            key_columns="payload",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert database.tables == {}
    assert database.live_mutations == []
    assert database.bound_rows == [(value,) for value in converted]
    pd.testing.assert_frame_equal(frame, original)


def test_keyed_gp_native_append_keeps_nullable_nonkey_partition_column(monkeypatch):
    database = native_cidr_target(monkeypatch)
    database.target.schema["month"] = "TEXT"
    for row in database.target.rows:
        row["month"] = None
    original_rows = [dict(row) for row in database.target.rows]
    frame = pd.DataFrame({"payload": ["192.0.2.0/24"], "month": [None]})
    original = frame.copy(deep=True)

    result = sql.load_df(
        "gp",
        "sandbox.events",
        frame,
        table_schema={"payload": "CIDR", "month": "TEXT"},
        write_mode="append",
        key_columns="payload",
        upsert_partition_column="month",
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result == 1
    assert database.target.rows == [
        *original_rows,
        {"payload": ipaddress.ip_network("192.0.2.0/24"), "month": None},
    ]
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize(
    ("native", "values", "stored"),
    [("BIGINT", ["01", "2"], [1, 2]), ("BOOLEAN", ["false", "t"], [False, True])],
)
def test_new_gp_upsert_keeps_distinct_converted_keys_and_bare_return(
    monkeypatch, native, values, stored
):
    database = effective_key_target(monkeypatch, native)
    database.tables.clear()
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    result = sql.load_df(
        "gp",
        "sandbox.events",
        frame,
        table_schema={"payload": native},
        write_mode="upsert",
        key_columns="payload",
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result == 2
    assert type(result) is int
    assert database.target.rows == [{"payload": value} for value in stored]
    assert set(database.tables) == {("sandbox", "events")}
    assert database.bound_rows == [(value,) for value in stored]
    pd.testing.assert_frame_equal(frame, original)
