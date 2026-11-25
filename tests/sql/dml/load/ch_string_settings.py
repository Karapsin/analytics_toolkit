"""Selected native String settings are certified before clearing live data."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql.backends.ch._support.native_string_insert import native_string_target


@pytest.mark.parametrize("native", ["String", "Array(String)"])
@pytest.mark.parametrize("encoding", ["ascii", "not-a-real-character-encoding"])
def test_native_string_encoding_rejects_late_unrepresentable_value_before_target_clear(
    monkeypatch, write_sql_connections, native, encoding
):
    store, endpoint = native_string_target(
        monkeypatch,
        {"payload": native},
        encoding=encoding,
        write_sql_connections=write_sql_connections,
    )
    values = ["first", "é"] if native == "String" else [["first"], ["é"]]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)
    before = deepcopy(store.rows)

    with pytest.raises(ValueError, match=r"selected ClickHouse.*encoder"):
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert store.rows == before
    assert store.schema == {"payload": native}
    assert store.submissions == []
    assert endpoint.total_insert_attempts == 0
    assert endpoint.opened
    assert all(item.close_calls == 1 for item in endpoint.opened)
    pd.testing.assert_frame_equal(frame, original)


def test_native_string_ascii_setting_keeps_valid_text_and_exact_bytes(
    monkeypatch, write_sql_connections
):
    store, endpoint = native_string_target(
        monkeypatch,
        {"payload": "String"},
        encoding="ascii",
        write_sql_connections=write_sql_connections,
    )
    frame = pd.DataFrame({"payload": pd.Series(["plain", b"\xff"], dtype=object)})
    original = frame.copy(deep=True)

    assert (
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )
        == 2
    )

    assert store.wire_columns == [("payload", "String", b"\x05plain\x01\xff")]
    assert endpoint.total_insert_attempts == 1
    assert endpoint.opened
    assert all(item.close_calls == 1 for item in endpoint.opened)
    pd.testing.assert_frame_equal(frame, original)
