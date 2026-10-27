"""Fail closed on malformed membership, probe responses and replica identities."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.outage_session import OutageSession, configured_cluster
from analytics_toolkit.sql.backends.ch.outage_topology import ClusterScope, TableObject
from analytics_toolkit.sql.backends.ch.routing import _RoutingClickHouse
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.execution.backend_options import BackendOperationState
from sqlglot import parse_one

from tests.sql._support.outage import Result


def _scope(client: Any, policy: str = "stop") -> ClusterScope:
    return ClusterScope(
        client._client, get_connection_config("ch"), "core", BackendOperationState(policy), policy
    )


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [()],
        [(1, 1, "h1", "invalid")],
        [(1, 1, "h1", 9000), (1, 1, "h2", 9000)],
        [(1, 1, "h1", 9000), (2, 1, "h1", 9000)],
    ],
)
def test_outage_membership_requires_distinct_complete_positions(outage: Any, rows: Any) -> None:
    cluster, _ = outage
    cluster.members = rows
    with pytest.raises(sql.ClickHouseClusterTopologyError):
        sql.read("ch", "SELECT id FROM db.events")


@pytest.mark.parametrize(
    "rows", [[()], [("x", "h1")], [(0, "h1")], [(3, "h1")], [(1, "h1"), (1, "h1")], [(1,)]]
)
def test_outage_probe_rejects_malformed_positions(outage: Any, rows: Any, monkeypatch: Any) -> None:
    client = sql.get_sql_connection("ch")
    original = client._client.query
    monkeypatch.setattr(
        client._client,
        "query",
        lambda query, **kwargs: (
            Result(rows) if "system, one" in query else original(query, **kwargs)
        ),
    )
    with pytest.raises(sql.ClickHouseClusterTopologyError):
        _scope(client)


@pytest.mark.parametrize(
    "rows",
    [
        [(1,)],
        [
            (
                0,
                "MergeTree",
                "MergeTree",
                "CREATE TABLE db.events (id UInt64) ENGINE=MergeTree ORDER BY id",
                "",
            )
        ],
        [
            (
                1,
                "MergeTree",
                "MergeTree",
                "CREATE TABLE db.events (id UInt64) ENGINE=MergeTree ORDER BY id",
                "",
            )
        ]
        * 2,
    ],
)
def test_outage_table_metadata_rejects_incomplete_or_duplicate_positions(
    outage: Any, rows: Any, monkeypatch: Any
) -> None:
    client = sql.get_sql_connection("ch")
    scope = _scope(client)
    original = client._client.query
    monkeypatch.setattr(
        client._client,
        "query",
        lambda query, **kwargs: (
            Result(rows) if "system, tables" in query else original(query, **kwargs)
        ),
    )
    with pytest.raises(sql.ClickHouseClusterTopologyError):
        scope.objects("db", "events")


@pytest.mark.parametrize(
    "replicas",
    [
        [],
        [(1, "/same", "r1", 0, 0), (2, "/same", "r2", 0, 0)],
        [(1, "/one", "r", 0, 0), (2, "/one", "r", 0, 0)],
    ],
)
def test_outage_replicas_require_metadata_and_distinct_shard_identities(
    outage: Any, replicas: Any, monkeypatch: Any
) -> None:
    cluster, _ = outage
    cluster.tables.clear()
    cluster.seed(replicated=True)
    client = sql.get_sql_connection("ch")
    scope = _scope(client)
    objects = scope.objects("db", "events_shard")
    original = client._client.query
    monkeypatch.setattr(
        client._client,
        "query",
        lambda query, **kwargs: (
            Result(replicas) if "system, replicas" in query else original(query, **kwargs)
        ),
    )
    with pytest.raises(sql.ClickHouseClusterTopologyError):
        scope.usable(objects, "db.events_shard")


def test_outage_replica_paths_must_agree_within_one_shard(outage: Any, monkeypatch: Any) -> None:
    cluster, _ = outage
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.tables.clear()
    cluster.seed(replicated=True)
    client = sql.get_sql_connection("ch")
    scope = _scope(client)
    objects = scope.objects("db", "events_shard")
    original = client._client.query
    rows = [(1, "/one", "r1", 0, 0), (2, "/different", "r2", 0, 0)]
    monkeypatch.setattr(
        client._client,
        "query",
        lambda query, **kwargs: (
            Result(rows) if "system, replicas" in query else original(query, **kwargs)
        ),
    )
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="identities"):
        scope.usable(objects, "db.events_shard")


def test_outage_no_table_ready_or_active_replica_is_an_error(outage: Any) -> None:
    cluster, _ = outage
    cluster.tables.clear()
    cluster.seed(replicated=True)
    cluster.replica_expired = {"h1", "h2"}
    scope = _scope(sql.get_sql_connection("ch"))
    with pytest.raises(sql.ClickHouseClusterUnavailableError, match="No table-ready"):
        scope.usable([], "db.events_shard")
    with pytest.raises(sql.ClickHouseClusterUnavailableError, match="No usable"):
        scope.usable(scope.objects("db", "events_shard"), "db.events_shard")


def test_outage_shared_table_uses_one_replica_per_shard(outage: Any) -> None:
    scope = _scope(sql.get_sql_connection("ch"))
    objects = [
        TableObject(host, "SharedMergeTree", "SharedMergeTree", "", "") for host in scope.hosts
    ]
    assert scope.usable(objects, "db.events_shard") == scope.hosts


def test_outage_settings_capability_is_detected(outage: Any, monkeypatch: Any) -> None:
    client = sql.get_sql_connection("ch")
    original = client._client.query
    monkeypatch.setattr(
        client._client,
        "query",
        lambda query, **kwargs: (
            Result([("skip_unavailable_shards_mode",)])
            if "FROM system.settings" in query
            else original(query, **kwargs)
        ),
    )
    scope = _scope(client)
    assert scope.probe_settings["skip_unavailable_shards_mode"] == "unavailable_or_table_missing"
    session = OutageSession(client._client, client._config, scope.state)
    session.scopes["core"] = scope
    kwargs: dict[str, Any] = {}
    client._settings(session, kwargs, {"skip_unavailable_shards": 0})
    assert kwargs["settings"]["skip_unavailable_shards_mode"] == "unavailable_or_table_missing"


def test_outage_local_facade_can_infer_routing_without_alias_override(outage: Any) -> None:
    _, configure = outage
    configure(cluster_routing=None)
    client = sql.get_sql_connection("ch")
    session = OutageSession(client._client, client._config, BackendOperationState())
    assert configured_cluster(client._config) == "core"
    route = session.infer_routing(parse_one("SELECT id FROM db.events", read=_RoutingClickHouse))
    assert route.cluster == "core"
    assert session.infer_routing(parse_one("SELECT 1", read=_RoutingClickHouse)) is None
    session.config = replace(session.config, database=None)
    assert (
        session.infer_routing(parse_one("SELECT * FROM missing", read=_RoutingClickHouse)) is None
    )
    assert (
        session.infer_routing(parse_one("SELECT * FROM system.tables", read=_RoutingClickHouse))
        is None
    )
    assert (
        session.infer_routing(parse_one("SELECT * FROM db.absent", read=_RoutingClickHouse)) is None
    )
