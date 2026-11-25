"""Public query creation preserves skip scope, metadata and return contracts."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table

from tests.sql._support.ch_lifecycle import install_lifecycle


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_query_if_not_exists_skips_an_existing_remote_physical_target(
    monkeypatch, write_sql_connections, driver
):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    table = "analytics.remote_existing"
    create = parse_create_table(
        f"CREATE TABLE {table} (id UInt64) ENGINE=MergeTree ORDER BY id", table
    )
    state._create(state.remote, create, [(value,) for value in range(100)])
    state.created.clear()
    before = state.snapshot()
    result = sql.create_table(
        "ch",
        table,
        sql="SELECT CAST(201 AS UInt64) AS would_populate",
        insert_data=False,
        if_not_exists=True,
        ch_distributed_table=False,
        ch_shard_on_cluster="storage",
        ch_engine="MergeTree",
        return_metadata=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert state.snapshot() == before
    assert state.commands == []
    assert result.rows is None
    assert result.metadata.statement_count == 0
    assert result.metadata.retry_attempts == 1
    assert all("would_populate" not in statement for statement in state.queries)
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("populate", [False, True], ids=["empty", "populated"])
def test_public_same_alias_query_creation_records_completed_operation_metadata(
    monkeypatch, write_sql_connections, driver, populate
):
    state = install_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    before = state.snapshot()
    table = "analytics.query_created"

    result = sql.create_table(
        "ch",
        table,
        sql="SELECT CAST(201 AS UInt64) AS id",
        insert_data=populate,
        ch_distributed_table=False,
        ch_shard_on_cluster="coordinator_only",
        ch_engine="MergeTree",
        order_by=["id"],
        return_metadata=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result.rows is None
    assert result.metadata.operation_status == "success"
    assert isinstance(result.metadata.elapsed_seconds, float)
    assert result.metadata.elapsed_seconds >= 0
    assert state.rows(state.coordinator, table) == ([(201,)] if populate else [])
    snapshot = state.snapshot()
    snapshot[state.coordinator].pop(table)
    assert snapshot == before
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)
