"""Existing facades cannot authorize destruction of independently owned data."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from tests.sql._support.ch_lifecycle import install_external_facade, install_lifecycle


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_public_truncate_insert_rejects_external_storage_before_any_mutation(
    monkeypatch, write_sql_connections, driver, explicit
):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    install_external_facade(state)
    before = state.snapshot()
    frame = pd.DataFrame({"id": pd.Series([201, 202], dtype="uint64")})
    original = frame.copy(deep=True)
    schema = {"table_schema": {"id": "UInt64"}} if explicit else {}

    with pytest.raises(InvalidSqlInputError, match=r"(externally owned|named explicitly)"):
        sql.load_df(
            "ch",
            "analytics.events",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
            **schema,
        )

    assert state.snapshot() == before
    assert state.commands == []
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)
    pd.testing.assert_frame_equal(frame, original)
