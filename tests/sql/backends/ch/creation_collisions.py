"""Desired companion collisions are rejected before public target recreation."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from tests.sql._support.ch_lifecycle import install_lifecycle


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("source", ["schema", "query"])
def test_public_drop_recreate_preserves_target_and_occupied_desired_companion(
    monkeypatch, write_sql_connections, driver, source
):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    target = "analytics.events"
    state.host_tables[state.coordinator].pop(target)
    local = parse_create_table(
        "CREATE TABLE analytics.events (id UInt64) ENGINE=MergeTree ORDER BY id", target
    )
    state._create(state.coordinator, local, [(1,), (2,), (3,)])
    state.host_tables[state.remote]["analytics.events_shard"].rows = [
        (value,) for value in range(100)
    ]
    state.created.clear()
    before = state.snapshot()
    options = (
        {"sql": "SELECT CAST(201 AS UInt64) AS id", "insert_data": False}
        if source == "query"
        else {"table_schema": {"id": "UInt64"}}
    )
    with pytest.raises(InvalidSqlInputError, match="companion"):
        sql.create_table(
            "ch",
            target,
            drop_if_exists=True,
            ch_distributed_table=True,
            ch_engine="MergeTree",
            ch_shard_on_cluster="storage",
            order_by=["id"],
            retry_cnt=1,
            timeout_increment=0,
            **options,
        )
    assert state.snapshot() == before
    assert state.commands == []
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)
