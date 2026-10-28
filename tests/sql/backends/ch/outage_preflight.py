"""The complete managed operation is validated before its first local mutation."""

from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.routing import local_sql


@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
def test_outage_public_drop_tables_preflights_before_local_drops(outage: Any, policy: str) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    if policy == "healthy_shards":
        with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
            sql.ClickHouseClusterUnavailableError
        ):
            sql.drop_tables("ch", "db.events", ch_cluster="core", ch_unavailable_shards=policy)
    else:
        with pytest.raises(sql.ClickHouseClusterUnavailableError):
            sql.drop_tables("ch", "db.events", ch_cluster="core")
    assert cluster.commands == []
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,)]


@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
def test_outage_local_control_cannot_bypass_cluster_destructive_preflight(
    outage: Any, policy: str
) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    client = sql.get_sql_connection("ch", ch_unavailable_shards=policy)
    with local_sql():
        if policy == "healthy_shards":
            with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
                sql.ClickHouseClusterUnavailableError
            ):
                client.command("DROP TABLE db.events_shard")
        else:
            with pytest.raises(sql.ClickHouseClusterUnavailableError):
                client.command("DROP TABLE db.events_shard")
    assert cluster.commands == []


@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
def test_outage_detach_is_destructive_and_never_queued(outage: Any, policy: str) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    client = sql.get_sql_connection("ch", ch_unavailable_shards=policy)
    if policy == "healthy_shards":
        with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
            sql.ClickHouseClusterUnavailableError
        ):
            client.command("DETACH TABLE db.events_shard ON CLUSTER core")
    else:
        with pytest.raises(sql.ClickHouseClusterUnavailableError):
            client.command("DETACH TABLE db.events_shard ON CLUSTER core")
    assert not cluster.commands


def test_outage_replica_failover_does_not_allow_partial_host_ddl(outage: Any) -> None:
    cluster, _ = outage
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.tables.clear()
    cluster.seed(replicated=True)
    cluster.alive.remove("h2")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        sql.ClickHouseClusterUnavailableError
    ):
        sql.execute(
            "ch",
            "TRUNCATE TABLE db.events_shard ON CLUSTER core",
            ch_unavailable_shards="healthy_shards",
        )
    assert cluster.commands == []


def test_outage_creation_validates_facade_scope_before_physical_create(outage: Any) -> None:
    cluster, _ = outage
    cluster.members_by_cluster["edge"] = [(1, 1, "h1", 9000), (2, 1, "h3", 9000)]
    with pytest.raises(sql.ClickHouseClusterUnavailableError):
        sql.create_table(
            "ch",
            "db.new_scope",
            table_schema={"id": "UInt64"},
            ch_shard_on_cluster="core",
            ch_distributed_on_cluster="edge",
            ch_distributed_cluster="core",
        )
    assert cluster.commands == []
    assert not any(name.startswith("db.new_scope") for _, name in cluster.tables)


def test_outage_missing_replica_schema_blocks_ddl_despite_logical_data_coverage(
    outage: Any,
) -> None:
    cluster, _ = outage
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.tables.clear()
    cluster.seed(replicated=True)
    del cluster.tables[("h2", "db.events_shard")]
    with pytest.raises(sql.ClickHouseClusterUnavailableError, match="readiness"):
        sql.execute(
            "ch",
            "TRUNCATE TABLE db.events_shard ON CLUSTER core",
            ch_unavailable_shards="healthy_shards",
        )
    assert not cluster.commands


def test_outage_local_only_batch_still_preflights_configured_scope(outage: Any) -> None:
    cluster, _ = outage
    client = sql.get_sql_connection("ch")
    client.sql_preflight_statements(["DROP TABLE db.events_shard"])
    assert any("system.clusters" in query for query in cluster.queries)
    assert not cluster.commands


def test_outage_unresolved_facade_routing_scope_rejected_before_first_batch_create(
    outage: Any,
) -> None:
    cluster, _ = outage
    client = sql.get_sql_connection("ch")
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="routing cluster"):
        client.sql_preflight_statements(
            [
                "CREATE TABLE db.new_shard ON CLUSTER core (id UInt64) "
                "ENGINE=MergeTree ORDER BY id",
                "CREATE TABLE db.new ON CLUSTER core (id UInt64) "
                "ENGINE=Distributed(concat('co','re'), 'db', 'new_shard', rand())",
            ]
        )
    assert not cluster.commands
