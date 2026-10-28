"""Deliberately rebuilt disposable snapshots must populate again before counting."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.execution.backend_options import backend_operation

from tests.sql._support.outage import Client
from tests.sql._support.outage_gp import MemoryGreenplum


@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
def test_outage_public_transfer_repopulates_snapshot_after_failed_first_population(
    outage: Any, monkeypatch: Any, policy: str
) -> None:
    cluster, configure = outage
    configure(
        aliases=("ch", "gp"),
        ch_unavailable_shards=policy,
        transfer_staging_schema="db",
        per_alias={"gp": {"type": "gp", "ddl_defaults": None, "cluster_routing": None}},
    )
    if policy == "healthy_shards":
        cluster.alive.remove("h2")
    greenplum = MemoryGreenplum()
    greenplum.install(monkeypatch)
    original = Client.insert
    original_command = Client.command
    attempts: list[tuple[str, str]] = []

    def insert(client: Any, table: str, *args: Any, **kwargs: Any) -> None:
        if "__source" in table:
            name, obj = next(
                (name, obj)
                for (host, name), obj in cluster.tables.items()
                if host == "h1" and "__source" in name
            )
            attempts.append((name, obj.uuid))
            original(client, table, *args, **kwargs)
            if len(attempts) == 1:
                msg = "disposable snapshot population interrupted after acceptance"
                raise ConnectionError(msg)
            return
        original(client, table, *args, **kwargs)

    def command(client: Any, query: str, **kwargs: Any) -> Any:
        result = original_command(client, query, **kwargs)
        if policy == "stop" and query.startswith("INSERT INTO") and "__source" in query:
            name, obj = next(
                (name, obj)
                for (host, name), obj in cluster.tables.items()
                if host == "h1" and "__source" in name
            )
            attempts.append((name, obj.uuid))
            if len(attempts) == 1:
                message = "disposable snapshot population interrupted after acceptance"
                raise ConnectionError(message)
        return result

    monkeypatch.setattr(Client, "insert", insert)
    monkeypatch.setattr(Client, "command", command)
    warning = (
        pytest.warns(sql.ClickHouseDegradedClusterWarning)
        if policy == "healthy_shards"
        else nullcontext()
    )
    with warning:
        result = sql.transfer(
            from_db="ch",
            to_db="gp",
            from_sql="SELECT id FROM db.events",
            to_table="db.retried",
            write_mode="replace",
            ignore_source_staging=False,
            return_metadata=True,
            table_schema={"id": "bigint"},
            retry_cnt=1,
            full_retry_cnt=2,
            timeout_increment=0,
            full_timeout_increment=0,
        )
    assert len(attempts) == 2
    assert attempts[0][0] == attempts[1][0]
    assert attempts[0][1] != attempts[1][1]
    expected = [1] if policy == "healthy_shards" else [1, 2]
    assert result.inserted_rows == len(expected)
    assert result.metadata.expected_source_rows == len(expected)
    assert result.metadata.row_count_validated is True
    assert greenplum.tables["db.retried"].id.tolist() == expected
    assert greenplum.tables["db.target"].id.tolist() == [99]
    assert not any("__source" in name for _host, name in cluster.tables)


def test_outage_truncating_owned_stage_preserves_its_existing_creation_identity(
    outage: Any,
) -> None:
    cluster, _ = outage
    table = "db.retry__stage__snapshot"
    with backend_operation("healthy_shards") as state:
        client = sql.get_sql_connection("ch")
        client.command(
            f"CREATE TABLE {table} ON CLUSTER core (id UInt64) ENGINE=MergeTree ORDER BY id"
        )
        client.insert(table, [(9,)], column_names=["id"])
        key = ("ch_create", "ch", "core", table)
        canonical = state.resources[key]
        identities = {host: cluster.tables[(host, table)].uuid for host in cluster.hosts}
        client.command(f"TRUNCATE TABLE {table} ON CLUSTER core")
        assert state.resources[key] == canonical
        assert all(cluster.tables[(host, table)].rows == [] for host in cluster.hosts)
        assert {host: cluster.tables[(host, table)].uuid for host in cluster.hosts} == identities


def test_outage_owned_local_stage_drop_executes_without_a_cluster_scope(outage: Any) -> None:
    cluster, configure = outage
    configure(cluster_routing=None, ddl_defaults=None)
    table = "db.local__stage__snapshot"
    with backend_operation("healthy_shards"):
        client = sql.get_sql_connection("ch")
        client.command(f"CREATE TABLE {table} (id UInt64) ENGINE=MergeTree ORDER BY id")
        assert ("h1", table) in cluster.tables
        client.command(f"DROP TABLE {table}")
    assert ("h1", table) not in cluster.tables
    assert cluster.commands[-1][1] == f"DROP TABLE {table}"
