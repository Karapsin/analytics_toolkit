"""Audit regressions for bound routing identities and effective coverage."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.managed_routing import (
    ManagedTableRoute,
    _ClusterObject,
    _normalized_definition,
)
from analytics_toolkit.sql.backends.ch.outage_session import OutageSession
from analytics_toolkit.sql.backends.ch.routing import ChClusterRouting, route_sql
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.execution.backend_options import BackendOperationState
from sqlglot import exp, parse_one


@pytest.mark.parametrize(
    "query",
    [
        "SELECT db.events.id, events.* FROM db.events",
        "SELECT db.events.id FROM db.events JOIN db.users u ON events.id = u.id",
        "SELECT events.id FROM db.events WHERE EXISTS "
        "(SELECT 1 FROM db.users u WHERE u.id=events.id)",
        "WITH events AS (SELECT id FROM db.raw) SELECT events.id, db.other.id "
        "FROM events JOIN db.other ON events.id=other.id",
        "SELECT one.dummy FROM system.one",
        "SELECT db.events.id FROM db.events WHERE EXISTS "
        "(SELECT 1 FROM other.events WHERE other.events.id=db.events.id)",
    ],
)
def test_routing_preserves_bound_qualified_columns_and_stars(query: str) -> None:
    routed = route_sql(query, routing=ChClusterRouting("core"), database="db")
    tree = parse_one(routed, read=ClickHouseDialect)
    for column in tree.find_all(exp.Column):
        assert not column.db
        assert not column.catalog
    for table in tree.find_all(exp.Table):
        if isinstance(table.this, exp.Anonymous) and table.alias == "events":
            assert table.this.expressions[2].this == "events"
    assert "randCanonical" not in routed


def test_routing_cte_shadowing_keeps_parent_and_nested_bindings_separate() -> None:
    query = (
        "SELECT events.id FROM db.events WHERE EXISTS "
        "(WITH events AS (SELECT id FROM db.raw) SELECT events.id FROM events)"
    )
    routed = route_sql(query, routing=ChClusterRouting("core"), database="db")
    tree = parse_one(routed, read=ClickHouseDialect)
    plain = [table for table in tree.find_all(exp.Table) if isinstance(table.this, exp.Identifier)]
    assert [table.name for table in plain] == ["events"]
    assert "cluster('core', 'db', 'events') AS _atk_source_" in routed
    assert "cluster('core', 'db', 'raw')" in routed


def test_two_argument_cluster_functions_parse_quoted_components_and_preserve_extras() -> None:
    resolver = SimpleNamespace(
        resolve=lambda **_kwargs: ManagedTableRoute("db.dot", "events.dot", "cluster")
    )
    routed = route_sql(
        "SELECT * FROM clusterAllReplicas('core', '`db.dot`.`events.dot`') AS e",
        routing=ChClusterRouting("core"),
        database="default",
        managed_pair_resolver=resolver,
    )
    assert "cluster('core', 'db.dot', 'events.dot') AS e" in routed
    routed = route_sql(
        "SELECT * FROM cluster('core','db.dot','events.dot',rand(), 'extra') AS e",
        routing=ChClusterRouting("core"),
        database="default",
        managed_pair_resolver=resolver,
    )
    function = parse_one(routed, read=ClickHouseDialect).find(exp.Anonymous)
    assert len(function.expressions) == 5
    assert function.expressions[3].name.lower() == "rand"
    assert function.expressions[4].this == "extra"


@pytest.mark.parametrize(
    "source",
    [
        "cluster('core', concat('db.', 'events'))",
        "cluster('core', db, concat('events', 'x'))",
        "cluster('core', 1, 2)",
    ],
)
def test_dynamic_cluster_identity_fails_before_submission(source: str) -> None:
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="static"):
        route_sql(f"SELECT * FROM {source}", routing=ChClusterRouting("core"), database="db")


def _definition(default: str, *, replica: str = "r1", uuid: str = "u1") -> _ClusterObject:
    ddl = (
        f"CREATE TABLE db.events UUID '{uuid}' (id UInt64, tag String DEFAULT '{default}') "
        f"ENGINE=ReplicatedMergeTree('/path', '{replica}') ORDER BY id"
    )
    return _ClusterObject("h", "ReplicatedMergeTree", "", ddl, uuid)


def test_canonical_ddl_preserves_literal_whitespace_but_ignores_permitted_identity() -> None:
    assert _normalized_definition(_definition("a  b")) != _normalized_definition(_definition("a b"))
    assert _normalized_definition(_definition("a  b")) == _normalized_definition(
        _definition("a  b", replica="r2", uuid="u2")
    )


def test_rand_and_randcanonical_remain_distinct_in_routed_defaults() -> None:
    query = (
        "CREATE TABLE db.events (id UInt32 DEFAULT rand(), "
        "value Float64 DEFAULT randCanonical()) ENGINE=MergeTree ORDER BY rand()"
    )
    routed = route_sql(query, routing=ChClusterRouting("core"), database="db")
    assert routed.count("rand()") == 2
    assert routed.count("randCanonical()") == 1


@pytest.mark.parametrize("driver", ["http", "native"])
def test_explicit_all_replicas_requires_physical_coverage_without_automatic_routing(
    outage: Any, driver: str
) -> None:
    cluster, configure = outage
    configure(driver=driver, cluster_routing=None)
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.tables.clear()
    cluster.seed(replicated=True)
    cluster.alive.remove("h2")
    with pytest.raises(sql.ClickHouseClusterUnavailableError):
        sql.read("ch", "SELECT id FROM clusterAllReplicas('core','db','events_shard')", retry_cnt=1)
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        result = sql.read(
            "ch",
            "SELECT id FROM clusterAllReplicas('core','db.events_shard')",
            ch_unavailable_shards="healthy_shards",
            return_metadata=True,
            retry_cnt=1,
        )
    assert result.metadata.data_complete is False
    assert any(
        item["coverage_intent"] == "physical_hosts" for item in result.metadata.ch_cluster_status
    )


def test_surviving_replica_in_every_shard_accepts_deterministic_write_key(outage: Any) -> None:
    cluster, configure = outage
    configure()
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.tables.clear()
    cluster.seed(replicated=True)
    cluster.alive.remove("h2")
    client = sql.get_sql_connection("ch")
    session = OutageSession(
        client._client, get_connection_config("ch"), BackendOperationState("healthy_shards")
    )
    scope = session.scope("core")
    session.insert_scope = scope
    session.insert_hosts = scope.reachable
    session.insert_key = "cityHash64(id)"
    assert session.insertion_settings()["skip_unavailable_shards"] == 0


def test_replica_inspection_keeps_dotted_database_as_one_identity(outage: Any) -> None:
    cluster, _configure = outage
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.tables.clear()
    for host in ("h1", "h2"):
        cluster.create(
            host,
            "CREATE TABLE `db.dot`.events UUID '11111111-1111-1111-1111-111111111111' "
            "(id UInt64) ENGINE=ReplicatedMergeTree('/dbdot/{shard}', '{replica}') ORDER BY id",
        )
    cluster.tables[("h1", "db.dot.events")].rows = [(42,)]
    result = sql.read("ch", "SELECT id FROM `db.dot`.events", retry_cnt=1)
    assert result.id.tolist() == [42]
    queries = [query for query in cluster.queries if "system, replicas" in query]
    assert queries
    assert all("database = 'db.dot'" in query and "table = 'events'" in query for query in queries)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1 UNION ALL SELECT 2 LIMIT 1 -- keep",
        "WITH q AS (SELECT 1) SELECT * FROM q UNION ALL SELECT 2 ORDER BY 1 OFFSET 1 -- keep",
        "SELECT 1 AS x ORDER BY x OFFSET 1 -- keep",
    ],
)
def test_count_limited_read_caps_outer_query_and_retains_existing_limits(query: str) -> None:
    adapter = get_backend_adapter("ch")
    limited = adapter.source_sql_for_count_limited_read(
        source_sql=query, expected_rows=42, enabled=True
    )
    tree = parse_one(limited, read=ClickHouseDialect)
    assert tree.args["limit"].expression.this == "42"
    assert "-- keep" in limited
    if "UNION" in query:
        inner = tree.find(exp.Union)
        assert inner is not None
        if "LIMIT 1" in query:
            assert inner.expression.args["limit"].expression.this == "1"
        assert limited.count("LIMIT") == (2 if "LIMIT 1" in query else 1)


def test_same_relation_name_in_correlated_scopes_gets_distinct_bound_aliases() -> None:
    query = (
        "SELECT db.events.id FROM db.events WHERE EXISTS "
        "(SELECT 1 FROM other.events WHERE other.events.id=db.events.id)"
    )
    tree = parse_one(
        route_sql(query, routing=ChClusterRouting("core"), database="db"), read=ClickHouseDialect
    )
    outer = tree.args["from_"].this
    inner = tree.find(exp.Exists).this
    nested = inner.args["from_"].this
    predicate = inner.args["where"].this
    assert outer.alias != nested.alias
    assert predicate.left.table == nested.alias
    assert predicate.right.table == outer.alias
