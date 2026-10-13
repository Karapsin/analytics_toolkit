"""The UI's default metadata boundary must not reach a SQL driver."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql_explorer import completion


def test_default_metadata_reads_are_empty_offline_and_reject_unfaked_user_queries() -> None:
    frame = completion._metadata_frame("gp", "SELECT schema_name")
    pd.testing.assert_frame_equal(frame, pd.DataFrame())
    with pytest.raises(AssertionError, match="require an explicit fake"):
        sql.read("gp", "SELECT user_data")


def test_explicit_metadata_fake_overrides_default_and_preserves_query_options(monkeypatch) -> None:
    rows = pd.DataFrame({"schema_name": ["sales"]})
    calls = []

    def read(db_key, query, **kwargs):
        calls.append((db_key, query, kwargs))
        return rows

    monkeypatch.setattr(sql, "read", read)
    assert completion._metadata_frame("warehouse", "SELECT schema_name") is rows
    assert calls[0][:2] == ("warehouse", "SELECT schema_name")
    assert calls[0][2]["retry_cnt"] == 1
    assert calls[0][2]["timeout_increment"] == 0
    assert calls[0][2]["query_label"].startswith("sql_explorer metadata=")
