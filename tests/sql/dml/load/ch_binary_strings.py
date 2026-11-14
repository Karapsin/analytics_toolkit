"""Mixed Unicode and binary String values retain their exact HTTP wire bytes."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.dml.load.load_sql_table import AmbiguousTableLoadError

from tests.sql._support.load_client_validation import client_target


@pytest.mark.parametrize("native_type", ["String", "Nullable(String)"])
def test_ch_binary_strings_mixed_unicode_and_bytes_keep_exact_http_encoding(
    monkeypatch, native_type
):
    store = client_target(monkeypatch, "http", {"payload": native_type})
    store.rows[:] = [{"payload": "seed"}]
    store.fail_generation = 0
    frame = pd.DataFrame({"payload": pd.Series(["Ω", b"\xff"], dtype=object)})
    original = frame.copy(deep=True)

    try:
        count = sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )
    except AmbiguousTableLoadError:
        # A real SDK failure is observable after CLEAR at this endpoint; the
        # desired contract is a successful exact native binary String write.
        assert store.rows == []
        assert [kind for kind, _detail in store.submissions] == ["CLEAR"]
        raise

    assert count == 2
    null_map = b"\x00\x00" if native_type.startswith("Nullable(") else b""
    expected = null_map + b"\x02\xce\xa9\x01\xff"
    assert store.wire_columns == [("payload", native_type, expected)]
    assert len(store.rows) == len(store.bound_rows) == 2
    assert store.rows == [{"payload": b"\xce\xa9"}, {"payload": b"\xff"}]
    assert store.schema == {"payload": native_type}
    assert [kind for kind, _detail in store.submissions] == ["CLEAR", "INSERT"]
    pd.testing.assert_frame_equal(frame, original)
