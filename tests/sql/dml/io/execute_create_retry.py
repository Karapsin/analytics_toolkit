"""Acknowledged CTAS resources do not suppress requested data replay."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table

from tests.sql._support.execute_create_retry import execute_create_retry_target


@pytest.mark.parametrize("if_not_exists", [False, True], ids=["default", "if-not-exists"])
@pytest.mark.parametrize(
    ("transport", "failure"),
    [("native", "before-write"), ("http", "lost-ack")],
)
def test_execute_create_retry_population_reuses_acknowledged_pair_and_replays_data(
    monkeypatch, write_sql_connections, transport, failure, if_not_exists
):
    state = execute_create_retry_target(monkeypatch, write_sql_connections, transport=transport)
    before = state.snapshot()
    state.population_failures = 1
    state.failure_mode = failure

    result = sql.execute_create(
        "ch",
        "analytics.retry_events",
        "SELECT 201 AS id",
        if_not_exists=if_not_exists,
        ch_distributed_table=True,
        ch_engine="MergeTree",
        ch_shard_on_cluster="storage",
        ch_distributed_cluster="storage",
        ch_distributed_on_cluster=None,
        retry_cnt=2,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows == result.metadata.inserted_rows == 1
    assert state.empty_before_population is True
    assert len(state.population_attempts) == 2
    first, second = (connection for connection, _sql in state.population_attempts)
    assert first is not second
    assert state.population_layouts[0].keys() == state.population_layouts[1].keys()
    assert {key: value["uuid"] for key, value in state.population_layouts[0].items()} == {
        key: value["uuid"] for key, value in state.population_layouts[1].items()
    }
    expected = [(201,)] * (2 if failure == "lost-ack" else 1)
    assert state.rows(state.coordinator, "analytics.retry_events") == expected
    assert state.rows(state.remote, "analytics.retry_events_shard") == expected
    after = state.snapshot()
    for host, tables in before.items():
        for name, original in tables.items():
            assert after[host][name] == original
    assert state.opened
    assert all(client.closed and client.close_calls == 1 for client in state.opened)


@pytest.mark.parametrize("transport", ["native", "http"])
def test_execute_create_retry_local_self_source_is_captured_before_destructive_ctas(
    monkeypatch, write_sql_connections, transport
):
    state = execute_create_retry_target(
        monkeypatch, write_sql_connections, transport=transport, local=True
    )
    state.host_tables[state.coordinator].pop("analytics.events")
    local = parse_create_table(
        "CREATE TABLE analytics.events (id UInt64) ENGINE=MergeTree ORDER BY id",
        "analytics.events",
    )
    state._create(state.coordinator, local, [(31,), (32,)])
    before = state.snapshot()
    independent = deepcopy(before[state.coordinator]["analytics.events_shard"])

    result = sql.execute_create(
        "ch",
        "analytics.events",
        "SELECT id FROM analytics.events",
        drop_if_exists=True,
        ch_distributed_table=False,
        ch_engine="MergeTree",
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows == result.metadata.input_count == 2
    assert state.source_scans == [("SELECT id FROM analytics.events", [(31,), (32,)])]
    assert state.events.index("input_complete") < state.events.index("mutation")
    assert state.rows(state.coordinator, "analytics.events") == [(31,), (32,)]
    after = state.snapshot()
    assert after[state.coordinator]["analytics.events_shard"] == independent
    assert len(state.rows(state.coordinator, "analytics.events_shard")) == 100
    assert after[state.remote] == before[state.remote]
    assert state.opened
    assert all(client.closed and client.close_calls == 1 for client in state.opened)
