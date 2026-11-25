"""Replicated routing proves shared live Keeper leases across actual peers."""

from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.metadata import (
    ClickHouseShardTable,
    build_clickhouse_shard_stats_query,
)
from analytics_toolkit.sql.backends.ch.outage_context import logical_statistics
from analytics_toolkit.sql.execution.backend_options import backend_operation

from tests.sql._support.ch_statistics_boundary import install_statistics_boundary
from tests.sql._support.outage import Client, ProgrammingError, Result


def _keeper_boundary(outage: Any, *, separate: bool) -> Any:
    cluster, _configure = outage
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.tables.clear()
    cluster.seed(replicated=True)
    cluster.keeper_domains = {"h1": "one", "h2": "two"} if separate else {}
    return cluster


def test_public_read_rejects_independent_keeper_namespaces_with_equal_replica_paths(
    outage: Any,
) -> None:
    cluster = _keeper_boundary(outage, separate=True)
    with pytest.raises(
        sql.ClickHouseClusterTopologyError, match=r"replication|Keeper|coordination"
    ):
        sql.read("ch", "SELECT id FROM db.events", retry_cnt=1)
    assert not cluster.selected_reads


def test_public_read_accepts_cross_peer_live_replica_identity_proof(outage: Any) -> None:
    cluster = _keeper_boundary(outage, separate=False)
    for host in ("h1", "h2"):
        cluster.tables[(host, "db.events_shard")].rows = [(9,)]
    result = sql.read("ch", "SELECT id FROM db.events", retry_cnt=1)
    assert result.id.tolist() == [9]


def test_public_read_uses_each_peers_actual_auxiliary_keeper_name(outage: Any) -> None:
    cluster = _keeper_boundary(outage, separate=False)
    cluster.host_keeper_names = {"h1": "coordinator_aux", "h2": "remote_aux"}
    for host in ("h1", "h2"):
        cluster.tables[(host, "db.events_shard")].rows = [(9,)]
    assert sql.read("ch", "SELECT id FROM db.events", retry_cnt=1).id.tolist() == [9]


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "malformed",
        "wrong_server",
        "not_ephemeral",
        "fractional_session",
        "different_lease",
        "denied",
    ],
)
def test_public_read_rejects_unverifiable_live_replication_identity(
    outage: Any, monkeypatch: Any, fault: str
) -> None:
    cluster = _keeper_boundary(outage, separate=False)
    original = Client.query

    def query(client: Any, statement: str, **kwargs: Any) -> Any:
        if "FROM system.zookeeper" not in statement:
            return original(client, statement, **kwargs)
        if fault == "denied":
            message = "SELECT denied for system.zookeeper"
            raise ProgrammingError(message)
        result = original(client, statement, **kwargs)
        value, owner = result.result_rows[0]
        if fault == "missing":
            rows = []
        elif fault == "malformed":
            rows = [("unsupported-active-identity", owner)]
        elif fault == "wrong_server":
            rows = [("UUID_'00000000-0000-0000-0000-000000000001'", owner)]
        elif fault == "not_ephemeral":
            rows = [(value, 0)]
        elif fault == "fractional_session":
            rows = [(value, 1.25)]
        else:
            rows = [(value, owner + (1 if client.host == "h2" else 0))]
        return Result(rows)

    monkeypatch.setattr(Client, "query", query)
    with pytest.raises(sql.ClickHouseClusterTopologyError, match=r"replication|Keeper|replica"):
        sql.read("ch", "SELECT id FROM db.events", retry_cnt=1)
    assert not cluster.selected_reads


def test_public_logical_statistics_reject_unshared_keeper_namespaces(
    outage: Any, monkeypatch: Any
) -> None:
    cluster = _keeper_boundary(outage, separate=True)
    server = install_statistics_boundary(monkeypatch, cluster)
    try:
        with pytest.raises(
            sql.ClickHouseClusterTopologyError, match=r"replication|Keeper|coordination"
        ):
            sql.show_tables("ch", schema="db", table_name="events_shard")
        assert not server.errors
    finally:
        server.close()


def test_public_logical_statistics_certify_shared_keeper_replica_identity(
    outage: Any, monkeypatch: Any
) -> None:
    cluster = _keeper_boundary(outage, separate=False)
    for host in ("h1", "h2"):
        cluster.tables[(host, "db.events_shard")].rows = [(9,)]
    server = install_statistics_boundary(monkeypatch, cluster)
    try:
        result = sql.show_tables("ch", schema="db", table_name="events_shard")
        assert not server.errors
        assert result.row_count.tolist() == [1]
        assert result.attrs.get("data_complete", True) is True
    finally:
        server.close()


def test_statistics_reuse_only_current_operation_object_and_replica_proof(
    outage: Any, monkeypatch: Any
) -> None:
    cluster = _keeper_boundary(outage, separate=False)
    server = install_statistics_boundary(monkeypatch, cluster)
    connection = sql.get_sql_connection("ch")
    statement = build_clickhouse_shard_stats_query(
        "core", {ClickHouseShardTable("core", "db", "events_shard")}
    )
    try:
        with backend_operation(), logical_statistics(targets=(("core", "db", "events_shard"),)):
            connection.query(statement)
            checked = sum("FROM system.zookeeper" in query for query in cluster.queries)
            assert checked > 0
            connection.query(statement)
            assert sum("FROM system.zookeeper" in query for query in cluster.queries) == checked
            cluster.tables.clear()
            cluster.seed(replicated=True)
            cluster.keeper_domains = {"h1": "one", "h2": "two"}
            with pytest.raises(sql.ClickHouseClusterTopologyError):
                connection.query(statement)
        assert not server.errors
    finally:
        connection.close()
        server.close()


def test_transient_keeper_probe_failure_retries_with_a_fresh_connection(
    outage: Any, monkeypatch: Any
) -> None:
    cluster = _keeper_boundary(outage, separate=False)
    for host in ("h1", "h2"):
        cluster.tables[(host, "db.events_shard")].rows = [(9,)]
    original = Client.query
    attempts: list[int] = []

    def query(client: Any, statement: str, **kwargs: Any) -> Any:
        if "FROM system.zookeeper" in statement:
            attempts.append(id(client))
            if len(attempts) == 1:
                message = "temporary probe transport failure"
                raise ConnectionError(message)
        return original(client, statement, **kwargs)

    monkeypatch.setattr(Client, "query", query)
    result = sql.read("ch", "SELECT id FROM db.events", retry_cnt=2, timeout_increment=0)
    assert result.id.tolist() == [9]
    assert len(attempts) >= 2
    assert attempts[0] != attempts[1]
