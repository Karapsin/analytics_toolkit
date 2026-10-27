"""Outage selection preserves engine, explicit route and replica semantics."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.outage_session import OutageSession, configured_cluster
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.ddl_defaults import _MISSING
from analytics_toolkit.sql.execution.backend_options import BackendOperationState

from tests.sql._support.outage import Result


def _facade(cluster: Any, engine: str) -> None:
    for host in cluster.hosts:
        item = cluster.tables[(host, "db.events")]
        item.ddl = item.ddl.replace(item.engine_full, engine)
        item.engine_full = engine


@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
def test_outage_system_metadata_and_missing_relations(outage: Any, policy: str) -> None:
    cluster, _ = outage
    if policy == "healthy_shards":
        cluster.alive.remove("h2")
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            sql.read("ch", "SELECT name FROM system.tables", ch_unavailable_shards=policy)
    else:
        sql.read("ch", "SELECT name FROM system.tables")
    with pytest.raises(sql.ClickHouseClusterUnavailableError, match="schema"):
        sql.read("ch", "SELECT id FROM db.absent", ch_unavailable_shards=policy)


@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
def test_outage_nonstandard_facade_is_local_only_in_strict_mode(outage: Any, policy: str) -> None:
    cluster, _ = outage
    _facade(cluster, "Distributed('core', 'db', 'custom_shard', rand())")
    if policy == "stop":
        sql.read("ch", "SELECT id FROM db.events")
        assert cluster.queries[-1] == "SELECT id FROM db.events"
    else:
        with pytest.raises(sql.ClickHouseClusterTopologyError, match="standard"):
            sql.read("ch", "SELECT id FROM db.events", ch_unavailable_shards=policy)


@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
def test_outage_different_data_routing_cluster_is_checked(outage: Any, policy: str) -> None:
    cluster, _ = outage
    _facade(cluster, "Distributed('other', 'db', 'events_shard', rand())")
    if policy == "stop":
        sql.read("ch", "SELECT id FROM db.events")
        assert cluster.queries[-1] == "SELECT id FROM db.events"
        assert any("'other'" in query for query in cluster.queries)
    else:
        with pytest.raises(sql.ClickHouseClusterTopologyError, match="agree"):
            sql.read("ch", "SELECT id FROM db.events", ch_unavailable_shards=policy)


def test_outage_absent_managed_physical_relation_is_not_an_empty_result(outage: Any) -> None:
    cluster, _ = outage
    for host in cluster.hosts:
        del cluster.tables[(host, "db.events_shard")]
    with pytest.raises(sql.ClickHouseClusterUnavailableError, match="absent"):
        sql.read("ch", "SELECT id FROM db.events")


@pytest.mark.parametrize("partial", [False, True])
def test_outage_views_require_complete_dependency_scope(outage: Any, partial: bool) -> None:
    cluster, _ = outage
    for host in cluster.hosts:
        cluster.tables[(host, "db.view")] = replace(
            cluster.tables[(host, "db.events_shard")],
            engine="View",
            engine_full="View",
            ddl="CREATE VIEW db.view AS SELECT id FROM db.events",
            rows=[],
        )
    if partial:
        cluster.alive.remove("h2")
        with pytest.raises(sql.ClickHouseClusterUnavailableError, match="View"):
            sql.read("ch", "SELECT id FROM db.view", ch_unavailable_shards="healthy_shards")
    else:
        sql.read("ch", "SELECT id FROM db.view")
        assert cluster.queries[-1] == "SELECT id FROM db.view"


@pytest.mark.parametrize("write", [False, True])
def test_outage_memory_tables_preserve_reads_and_reject_degraded_writes(
    outage: Any, write: bool
) -> None:
    cluster, _ = outage
    for host in cluster.hosts:
        item = cluster.tables[(host, "db.events_shard")]
        item.engine = item.engine_full = "Memory"
        item.ddl = item.ddl.replace("MergeTree", "Memory")
    assert sql.read("ch", "SELECT id FROM db.events").id.tolist() == [1, 2]
    cluster.alive.remove("h2")
    if write:
        with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
            sql.ClickHouseClusterTopologyError, match="engine"
        ):
            sql.execute(
                "ch", "INSERT INTO db.events VALUES (9)", ch_unavailable_shards="healthy_shards"
            )
        assert not cluster.commands
    else:
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            result = sql.read(
                "ch", "SELECT id FROM db.events", ch_unavailable_shards="healthy_shards"
            )
        assert result.id.tolist() == [1]


def test_outage_facade_outside_physical_scope_remains_local(outage: Any, monkeypatch: Any) -> None:
    cluster, _ = outage
    client = sql.get_sql_connection("ch")
    original = client._client.query
    monkeypatch.setattr(
        client._client,
        "query",
        lambda query, **kwargs: (
            Result([])
            if "system, tables" in query and "name = 'events'" in query
            else original(query, **kwargs)
        ),
    )
    assert client.outage_table_exists("db.events")
    client.query("SELECT id FROM db.events")
    assert cluster.queries[-1] == "SELECT id FROM db.events"


def test_outage_direct_physical_write_repairs_recovered_schema(outage: Any) -> None:
    cluster, _ = outage
    del cluster.tables[("h2", "db.events_shard")]
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        sql.execute(
            "ch", "INSERT INTO db.events_shard VALUES (9)", ch_unavailable_shards="healthy_shards"
        )
    assert cluster.tables[("h2", "db.events_shard")].rows == []


def test_outage_independent_replicas_use_original_positions_for_batches(outage: Any) -> None:
    cluster, _ = outage
    cluster.hosts.append("h3")
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000), (2, 1, "h3", 9000)]
    cluster.alive = {"h1", "h3"}
    cluster.tables.clear()
    cluster.seed()
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        sql.execute(
            "ch",
            "INSERT INTO db.events VALUES (9); INSERT INTO db.events VALUES (10)",
            ch_unavailable_shards="healthy_shards",
        )
    inserts = [settings for _, query, settings in cluster.commands if query.startswith("INSERT")]
    assert [settings["insert_shard_id"] for settings in inserts] == [1, 3]
    assert cluster.tables[("h3", "db.events_shard")].rows == [(10,)]


def test_outage_invalid_effective_key_is_not_assumed_random(outage: Any) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    client = sql.get_sql_connection("ch")
    session = OutageSession(client._client, client._config, BackendOperationState("healthy_shards"))
    scope = session.scope("core")
    session.insert_scope, session.insert_hosts, session.insert_key = scope, scope.reachable, "CASE"
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="verify"):
        session.insertion_settings()


def test_outage_cluster_default_can_be_absent(outage: Any) -> None:
    _, configure = outage
    configure(cluster_routing=None, ddl_defaults=None)
    assert configured_cluster(get_connection_config("ch")) is None


@pytest.mark.parametrize("facade_scope", [None, "{cluster}", _MISSING])
def test_outage_recovery_honors_explicit_local_and_inferred_facade_scope(
    outage: Any, facade_scope: Any
) -> None:
    cluster, _ = outage
    del cluster.tables[("h2", "db.events_shard")]
    del cluster.tables[("h2", "db.events")]
    client = sql.get_sql_connection("ch", ch_unavailable_shards="healthy_shards")
    defaults = client._config.ddl_defaults
    regular = replace(
        defaults.regular, distributed=replace(defaults.regular.distributed, on_cluster=facade_scope)
    )
    client._config = replace(client._config, ddl_defaults=replace(defaults, regular=regular))
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        client.command("INSERT INTO db.events VALUES (9)")
    assert ("h2", "db.events_shard") in cluster.tables
    assert (("h2", "db.events") in cluster.tables) is (facade_scope is not None)


@pytest.mark.parametrize("binary", [False, True])
def test_outage_delivery_uses_the_verified_managed_facade_key(outage: Any, binary: bool) -> None:
    cluster, configure = outage
    configure(cluster_routing={"cluster": "core", "sharding_key": "id"})
    client = sql.get_sql_connection("ch")
    if binary:
        client.insert("db.events", [(9,)], column_names=["id"])
    else:
        client.command("INSERT INTO db.events VALUES (9)")
    assert "RAND()" in cluster.commands[-1][1].upper()
