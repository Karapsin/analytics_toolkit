"""Catalog naming patterns never authorize deletion of replacement artifacts."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError

from tests.sql._support.ch_lifecycle import install_lifecycle


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("scope", ["local", "storage"])
def test_public_replacement_preserves_unowned_discardable_named_table(
    monkeypatch, write_sql_connections, driver, scope
):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    target = "analytics.events" if scope == "local" else "analytics.events_shard"
    orphan = target + "__replace_guarded_manual__discardable"
    create = parse_create_table(
        f"CREATE TABLE {orphan} (id UInt64) ENGINE=MergeTree ORDER BY id", orphan
    )
    host = state.coordinator if scope == "local" else state.remote
    state._create(host, create, [(value,) for value in range(100)])
    state.created.clear()
    before = state.snapshot()
    incoming = pd.DataFrame({"id": pd.Series([201, 202], dtype="uint64")})
    original = incoming.copy(deep=True)
    error = None

    try:
        sql.load_df(
            "ch",
            "analytics.events",
            incoming,
            table_schema={"id": "UInt64"},
            write_mode="replace",
            retry_cnt=1,
            timeout_increment=0,
        )
    except AmbiguousSqlReplaceError as caught:
        error = caught

    current = state.snapshot()
    for endpoint, tables in before.items():
        assert {name: current[endpoint].get(name) for name in tables} == tables
    assert isinstance(error, AmbiguousSqlReplaceError), repr(error)
    assert "unresolved artifact" in str(error)
    assert orphan in str(error)
    assert not [name for _host, name in state.created if "__replace_guarded_" in name]
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)
    pd.testing.assert_frame_equal(incoming, original)
