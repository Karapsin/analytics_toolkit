"""Indirect relations cannot hide selected input from destructive query creation."""

from __future__ import annotations

import struct

from analytics_toolkit import sql

from tests.sql._support.query_indirect_source import indirect_ch_pair, indirect_trino_view


def test_query_indirect_source_distributed_alias_freezes_shared_storage_before_drop(
    monkeypatch, write_sql_connections
):
    state = indirect_ch_pair(monkeypatch, write_sql_connections)
    before = state.snapshot()
    independent = before[state.coordinator]["analytics.events_shard"]
    source_alias = before[state.coordinator]["analytics.events_alias"]

    result = sql.create_table(
        "ch",
        "analytics.events",
        sql="SELECT id FROM analytics.events_alias",
        insert_data=True,
        drop_if_exists=True,
        ch_distributed_table=True,
        ch_engine="MergeTree",
        ch_shard_on_cluster="storage",
        ch_distributed_on_cluster=None,
        ch_distributed_cluster="storage",
        ch_sharding_key="rand()",
        order_by="id",
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    assert state.rows(state.coordinator, "analytics.events") == [(1,), (2,)]
    assert state.rows(state.remote, "analytics.events_shard") == [(1,), (2,)]
    assert result.metadata.input_count == result.metadata.inserted_rows == 2
    assert state.source_scans == [("SELECT id FROM analytics.events_alias", [(1,), (2,)])]
    assert state.source_events.index("input_complete") < state.source_events.index("mutation")
    after = state.snapshot()
    assert after[state.coordinator]["analytics.events_shard"] == independent
    assert len(state.rows(state.coordinator, "analytics.events_shard")) == 100
    assert after[state.coordinator]["analytics.events_alias"] == source_alias
    assert state.native_wire == [struct.pack("<2q", 1, 2)]
    assert state.opened
    assert all(client.closed and client.close_calls == 1 for client in state.opened)


def test_query_indirect_source_trino_view_freezes_target_dependency_before_drop(
    monkeypatch, write_sql_connections, tmp_path
):
    database = indirect_trino_view(monkeypatch, write_sql_connections, tmp_path)

    result = sql.create_table(
        "trino",
        "iceberg.sandbox.events",
        sql="SELECT id FROM iceberg.sandbox.events_view",
        insert_data=True,
        drop_if_exists=True,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows == result.metadata.input_count == 2
    assert database.rows == [(1,), (2,)]
    assert database.reads == database.probes == 1
    assert database.events.index("input_complete") < database.events.index("drop")
    assert database.events.count("create") == 1
    assert database.server_copies == []
    assert [params for _client, _sql, params, _wire in database.insert_attempts] == [[1, 2]]
    assert [wire for _client, _sql, _params, wire in database.insert_attempts] == [["1", "2"]]
    assert database.view_definition == "SELECT id FROM iceberg.sandbox.events"
    assert database.opened
    assert all(client.close_calls == 1 for client in database.opened)
