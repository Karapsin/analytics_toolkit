"""Acknowledged facade queues and lagging storage survive structural changes."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from tests.sql._support.ch_recovery_inventory import command_parts
from tests.sql._support.ch_replica_lifecycle import install_replica_lifecycle


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("queued_host", ["coordinator", "remote"])
def test_public_facade_reconfiguration_preserves_acknowledged_pending_insert_queue(
    monkeypatch, write_sql_connections, driver, queued_host
):
    state = install_replica_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    queued = [(901,), (902,)]
    state.queue_rows[queued_host, "analytics.events"] = list(queued)
    state.host_tables[queued_host]["analytics.events"].rows = list(queued)
    before = state.snapshot()
    with pytest.raises(InvalidSqlInputError, match="queue"):
        sql.ch_reconfigure_table(
            "ch",
            "analytics.events",
            ch_sharding_key="cityHash64(id)",
            ch_distributed_on_cluster="core",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert state.snapshot() == before
    assert state.commands == []
    assert state.queue_rows[queued_host, "analytics.events"] == queued
    assert all(connection.close_calls == 1 for connection in state.opened)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_facade_reconfiguration_accepts_verified_empty_insert_queues(
    monkeypatch, write_sql_connections, driver
):
    state = install_replica_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    before = state.snapshot()

    result = sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        ch_sharding_key="cityHash64(id)",
        ch_distributed_on_cluster="core",
        return_metadata=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result.data["strategy"] == "wrapper_recreate"
    assert result.metadata.final_target_rows == 3
    assert result.metadata.row_count_validated is True
    for host in state.host_tables:
        assert state.rows(host, "analytics.events") == [(11,), (12,), (13,)]
        assert (
            state.snapshot()[host]["analytics.events_shard"]
            == before[host]["analytics.events_shard"]
        )
    assert (
        state.snapshot()[state.coordinator]["analytics.unrelated"]
        == before[state.coordinator]["analytics.unrelated"]
    )
    assert all(connection.close_calls == 1 for connection in state.opened)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_reconfiguration_synchronizes_every_replacement_replica_before_cutover(
    monkeypatch, write_sql_connections, driver
):
    state = install_replica_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    before = state.snapshot()

    result = sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        order_by="id DESC",
        return_metadata=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result.data["strategy"] == "managed_pair_rebuild"
    assert result.data["cleanup_complete"] is True
    assert result.metadata.source_rows == result.metadata.staged_rows == 3
    assert result.metadata.final_target_rows == 3
    assert result.metadata.row_count_validated is True
    assert len(state.syncs) == 1
    replacement, scope, sync_position = state.syncs[0]
    assert replacement.startswith("analytics.events_shard__reconfigure_")
    assert scope == "storage"
    cutovers = [
        index
        for index, statement in enumerate(state.commands, 1)
        if command_parts(statement)[0] in {"EXCHANGE", "RENAME"}
    ]
    assert cutovers
    assert sync_position < min(cutovers)
    for host in state.host_tables:
        assert state.rows(host, "analytics.events") == [(11,), (12,), (13,)]
        assert state.host_tables[host]["analytics.events_shard"].rows == [(11,), (12,), (13,)]
    assert state.replication_pending == {}
    assert (
        state.snapshot()[state.coordinator]["analytics.unrelated"]
        == before[state.coordinator]["analytics.unrelated"]
    )
    assert not any(
        "__reconfigure_" in name or "__copy_" in name
        for tables in state.host_tables.values()
        for name in tables
    )
    assert all(connection.close_calls == 1 for connection in state.opened)
