"""Verified target storage and independently routed stages keep separate identities."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.layout import ExistingClickHouseLayout
from analytics_toolkit.sql.backends.ch.managed_routing import ManagedPairResolver, ManagedTableRoute
from analytics_toolkit.sql.backends.ch.routing import ChClusterRouting, ClusterRoutingClient
from analytics_toolkit.sql.backends.ch.upsert_storage import (
    apply_upsert_write_key,
    current_upsert_storage,
    upsert_storage_resolver,
    upsert_write_scope,
    verified_upsert_storage,
)
from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError
from sqlglot import exp, parse_one


def layout(cluster=None, *, facade=False, physical="db.target", logical="db.target"):
    return ExistingClickHouseLayout(
        logical_table=logical,
        physical_table=physical,
        create_distributed_pair=facade,
        shard_on_cluster=cluster,
        distributed_on_cluster=None,
        distributed_cluster=cluster if facade else None,
        ddl="verified existing definition",
        physical_ddl="verified physical definition",
    )


class Resolver:
    def __init__(self, mode="all_replicas"):
        self.calls = []
        self.mode = mode
        self.invalidated = False

    def invalidate(self):
        self.invalidated = True

    def resolve(self, *, cluster, database, table):
        self.calls.append((cluster, database, table))
        return ManagedTableRoute(database, table, self.mode, sharding_key="caller_key")

    resolve_physical = resolve


def client():
    wrapped = ClusterRoutingClient(
        object(), routing=ChClusterRouting("A", "caller_key"), database="db"
    )
    resolver = Resolver()
    object.__setattr__(wrapped, "_managed_pairs", resolver)
    return wrapped, resolver


def functions(tree):
    return [
        (node.name.lower(), *(item.this for item in node.expressions[:3]))
        for node in tree.find_all(exp.Anonymous)
        if node.name.lower() in {"cluster", "clusterallreplicas"}
    ]


@pytest.mark.parametrize("cluster", [None, "A", "B"])
def test_verified_upsert_storage_routes_target_and_stages_independently(cluster):
    wrapped, resolver = client()
    existing = layout(cluster, facade=cluster == "B", physical="db.target")
    with verified_upsert_storage(wrapped, existing, sharding_key="actual_key"):
        preserve = parse_one(
            wrapped.route(
                "INSERT INTO db.final_stage SELECT dst.id FROM db.target dst "
                "LEFT JOIN db.incoming_stage src ON dst.id=src.id"
            ),
            read=ClickHouseDialect,
        )
        publish = parse_one(
            wrapped.route("INSERT INTO db.target SELECT id FROM db.final_stage"),
            read=ClickHouseDialect,
        )
    expected = ("clusterallreplicas", cluster, "db", "target")
    assert (expected in functions(preserve)) == (cluster is not None)
    assert (expected in functions(publish)) == (cluster is not None)
    assert ("clusterallreplicas", "A", "db", "final_stage") in functions(preserve)
    assert ("clusterallreplicas", "A", "db", "final_stage") in functions(publish)
    assert ("clusterallreplicas", "A", "db", "incoming_stage") in functions(preserve)
    if cluster is None:
        assert any(table.name == "target" for table in preserve.find_all(exp.Table))
        assert publish.this.name == "target"
        assert all(table != "target" for _, _, table in resolver.calls)
    else:
        assert (cluster, "db", "target") in resolver.calls
        assert publish.this.expressions[-1].sql(dialect="clickhouse") == "actual_key"


def test_verified_upsert_storage_physical_identity_and_missing_facade_key_are_authoritative():
    wrapped, resolver = client()
    existing = layout("B", facade=True, physical='"Other.DB"."Physical"', logical="target")
    with verified_upsert_storage(wrapped, existing):
        sql = wrapped.route('INSERT INTO "Other.DB".target SELECT * FROM "Other.DB".Physical')
        tree = parse_one(sql, read=ClickHouseDialect)
        assert len(tree.this.expressions) == 3
        assert functions(tree) == [
            ("clusterallreplicas", "B", "Other.DB", "Physical"),
            ("clusterallreplicas", "B", "Other.DB", "Physical"),
        ]
        assert current_upsert_storage(object()) is None
    assert current_upsert_storage(wrapped) is None
    assert resolver.calls == [("B", "Other.DB", "Physical")] * 2


def test_verified_upsert_storage_does_not_capture_other_namespaces_ctes_or_connections():
    wrapped, resolver = client()
    other, _ = client()
    with verified_upsert_storage(wrapped, layout()):
        wrong_namespace = wrapped.route("SELECT * FROM other.target")
        assert "clusterAllReplicas('A', 'other', 'target')" in wrong_namespace
        assert "clusterAllReplicas('A', 'db', 'target')" in other.route("SELECT * FROM db.target")
        query = wrapped.route("WITH target AS (SELECT 1 AS id) SELECT * FROM target")
        assert "cluster" not in query.lower()
        assert upsert_storage_resolver(object(), resolver, "caller") is resolver


def test_verified_upsert_storage_resets_after_nested_exception_and_isolates_threads():
    connection = object()
    with verified_upsert_storage(connection, layout()):
        outer = current_upsert_storage(connection)

        def fail():
            with verified_upsert_storage(connection, layout("B")):
                message = "inner"
                raise RuntimeError(message)

        with pytest.raises(RuntimeError, match="inner"):
            fail()
        assert current_upsert_storage(connection) is outer
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(current_upsert_storage, connection).result() is None
    assert current_upsert_storage(connection) is None
    invalid = verified_upsert_storage(connection, layout(physical="numbers(1)"))
    with pytest.raises(ValueError, match="static database/table"), invalid:
        pytest.fail("dynamic target accepted")


def test_verified_upsert_storage_resolver_checks_actual_physical_coverage_before_routing():
    connection = object()
    original = Resolver(mode="local")
    with verified_upsert_storage(connection, layout("B")):
        resolver = upsert_storage_resolver(connection, original, "caller")
        with pytest.raises(ClickHouseClusterTopologyError, match="complete routed"):
            resolver.resolve(cluster="A", database="db", table="target")
        resolver.invalidate()
        assert original.invalidated


@pytest.mark.parametrize("cluster", [None, "B"])
def test_verified_upsert_storage_write_availability_scope_matches_actual_target(cluster):
    connection = object()
    statement = parse_one("INSERT INTO db.target SELECT * FROM db.stage", read=ClickHouseDialect)
    routing = ChClusterRouting("A", "caller_key")
    with verified_upsert_storage(
        connection, layout(cluster, physical="db.physical"), sharding_key="actual"
    ):
        resolved, target = upsert_write_scope(connection, statement, routing, ("db", "target"))
        assert target == ("db", "physical")
        assert (resolved.cluster if resolved else None) == cluster
        if resolved:
            assert resolved.sharding_key == "actual"
        assert upsert_write_scope(connection, statement, routing, ("other", "target")) == (
            routing,
            ("other", "target"),
        )
        assert upsert_write_scope(connection, statement, routing, None) == (routing, None)
        assert upsert_write_scope(connection, exp.select("1"), routing, ("db", "target")) == (
            routing,
            ("db", "target"),
        )
    assert upsert_write_scope(connection, statement, routing, ("db", "target")) == (
        routing,
        ("db", "target"),
    )


@pytest.mark.parametrize("engine", ["MergeTree", "ReplicatedMergeTree", "View"])
def test_verified_upsert_storage_physical_resolver_uses_remote_engine_not_local_facade(engine):
    resolver = ManagedPairResolver(object())
    resolver._read_consistent_cluster_object = lambda **_kwargs: (
        [SimpleNamespace(engine=engine)],
        {1: 2},
    )
    if engine == "View":
        with pytest.raises(ClickHouseClusterTopologyError, match="retain its MergeTree"):
            resolver.resolve_physical(cluster="B", database="db", table="target")
    else:
        resolved = resolver.resolve_physical(cluster="B", database="db", table="target")
        assert resolved.mode == ("all_replicas" if engine == "MergeTree" else "cluster")


@pytest.mark.parametrize("facade", [False, True])
@pytest.mark.parametrize("key", [None, "actual_key"])
def test_verified_upsert_storage_outage_settings_keep_actual_facade_key(facade, key):
    connection = object()
    session = SimpleNamespace(insert_key="caller_key")
    with verified_upsert_storage(connection, layout("B", facade=facade), sharding_key=key):
        apply_upsert_write_key(connection, session, None)
        apply_upsert_write_key(connection, session, ("other", "target"))
        assert session.insert_key == "caller_key"
        apply_upsert_write_key(connection, session, ("db", "target"))
        assert session.insert_key == (key if facade else "caller_key")
    session.insert_key = "outside"
    apply_upsert_write_key(connection, session, ("db", "target"))
    assert session.insert_key == "outside"
