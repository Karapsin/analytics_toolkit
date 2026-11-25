"""Metadata-proven CH query population keeps acknowledged creation through replay."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql

from tests.sql._support.execute_create_retry import execute_create_retry_target


@pytest.mark.parametrize(
    ("transport", "failure"),
    [("native", "before-write"), ("http", "lost-ack")],
)
def test_native_population_retry_keeps_acknowledged_pair_and_selected_server_copy(
    monkeypatch, write_sql_connections, transport, failure
):
    state = execute_create_retry_target(monkeypatch, write_sql_connections, transport=transport)
    before = state.snapshot()
    state.population_failures = 1
    state.failure_mode = failure

    result = sql.create_table(
        "ch",
        "analytics.retry_events",
        sql="SELECT CAST(201 AS UInt64) AS id",
        insert_data=True,
        ch_distributed_table=True,
        ch_engine="MergeTree",
        ch_shard_on_cluster="storage",
        ch_distributed_cluster="storage",
        ch_distributed_on_cluster=None,
        order_by="id",
        retry_cnt=2,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows is None
    assert result.metadata.data_write_replay_possible is True
    assert state.source_scans == []
    assert state.empty_before_population is True
    assert len(state.population_attempts) == 2
    first, second = (connection for connection, _statement in state.population_attempts)
    assert first is not second
    assert {key: value["uuid"] for key, value in state.population_layouts[0].items()} == {
        key: value["uuid"] for key, value in state.population_layouts[1].items()
    }
    expected = [(201,)] * (2 if failure == "lost-ack" else 1)
    assert state.rows(state.coordinator, "analytics.retry_events") == expected
    assert state.rows(state.remote, "analytics.retry_events_shard") == expected
    assert all(
        "SELECT CAST(201 AS UInt64) AS id" in statement
        for _connection, statement in state.population_attempts
    )
    after = state.snapshot()
    for host, tables in before.items():
        for name, original in tables.items():
            assert after[host][name] == original
    assert state.opened
    assert all(client.closed and client.close_calls == 1 for client in state.opened)
