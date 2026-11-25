from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.connection.errors import (
    ClickHouseClusterTopologyError,
    InvalidSqlInputError,
)
from sqlglot import exp, parse_one

layout = importlib.import_module("analytics_toolkit.sql.backends.ch.layout")
outage = importlib.import_module("analytics_toolkit.sql.backends.ch.outage_layout")


@pytest.mark.parametrize(
    ("scope", "resolved"), [(None, None), ("missing", None), ("cluster", "real")]
)
def test_layout_show_failure_uses_only_a_resolved_explicit_facade_scope(
    monkeypatch, scope, resolved
):
    failure = OSError("coordinator absent")
    calls = []

    def local(*_args):
        raise failure

    monkeypatch.setattr(layout, "show_create_table", local)
    monkeypatch.setattr(layout, "resolve_optional_cluster", lambda *_a: resolved)
    ddl = "CREATE TABLE db.events (id Int64) ENGINE = MergeTree ORDER BY id"
    monkeypatch.setattr(
        layout, "show_create_table_on_cluster", lambda *_a: calls.append(_a[-1]) or ddl
    )
    if resolved is None:
        with pytest.raises(OSError, match="coordinator absent") as caught:
            layout.inspect_existing_layout(object(), "db.events", distributed_on_cluster=scope)
        assert caught.value is failure
        assert calls == []
    else:
        result = layout.inspect_existing_layout(object(), "db.events", distributed_on_cluster=scope)
        assert result.physical_ddl == ddl
        assert calls == ["real"]


@pytest.mark.parametrize(
    ("reference", "resolved"),
    [
        (None, "core"),
        (SimpleNamespace(database="db", table="events_shard", cluster="unknown"), None),
    ],
)
def test_layout_rejects_unverifiable_distributed_reference_before_storage_queries(
    monkeypatch, reference, resolved
):
    ddl = (
        "CREATE TABLE db.events (id Int64) ENGINE = Distributed('core','db','events_shard',rand())"
    )
    monkeypatch.setattr(layout, "show_create_table", lambda *_a: ddl)
    monkeypatch.setattr(layout, "extract_clickhouse_distributed_shard_table", lambda *_a: reference)
    monkeypatch.setattr(layout, "resolve_optional_cluster", lambda *_a: resolved)
    monkeypatch.setattr(
        layout, "show_create_table_on_cluster", lambda *_a: pytest.fail("storage read")
    )
    with pytest.raises(InvalidSqlInputError, match=r"Cannot (verify|resolve)"):
        layout.inspect_existing_layout(object(), "db.events")


def test_outage_layout_hook_missing_table_never_falls_back_to_routed_data_reads():
    connection = SimpleNamespace(outage_inspect_existing_layout=lambda _: None)
    with pytest.raises(InvalidSqlInputError, match="does not exist"):
        layout.inspect_existing_layout(connection, "db.absent")


def session_for(objects, *, storage=None, database="db", routing=False, fallback=None):
    config = SimpleNamespace(database=database, cluster_routing=routing)
    scope = SimpleNamespace(objects=lambda _db, table: objects if table == "events" else storage)
    return SimpleNamespace(
        config=config,
        client=SimpleNamespace(query=lambda *_a, **_k: SimpleNamespace(result_rows=[])),
        scope=lambda _cluster: scope,
        local_objects=lambda *_a: objects,
        local_facade=lambda *_a: fallback,
    )


def table(
    engine="Distributed",
    engine_full="Distributed('core','db','events_shard',rand())",
    *,
    table_name="events",
):
    expression = engine_full if engine == "Distributed" else engine
    order = " ORDER BY id" if engine.lower().endswith("mergetree") else ""
    return SimpleNamespace(
        engine=engine,
        engine_full=expression,
        create_sql=f"CREATE TABLE db.{table_name} (id Int64) ENGINE={expression}{order}",
    )


def test_outage_layout_requires_database_and_keeps_absent_objects_absent():
    with pytest.raises(ClickHouseClusterTopologyError, match="current database identity"):
        outage.inspect_outage_layout(session_for([], database=None), "events")
    assert outage.inspect_outage_layout(session_for([]), "db.events") is None


def test_outage_local_object_and_local_facade_fallback_keep_exact_named_identity():
    result = outage.inspect_outage_layout(session_for([table("MergeTree")]), "db.events")
    assert result.physical_table == result.logical_table == "db.events"
    assert result.create_distributed_pair is False
    result = outage.inspect_outage_layout(
        session_for([], fallback=[table("MergeTree")]), "db.events"
    )
    assert result.physical_table == "db.events"


@pytest.mark.parametrize("engine", [None, "absent", "Memory", "Log", "Distributed"])
def test_outage_layout_ownership_distinguishes_absence_from_direct_storage(engine):
    storage = (
        None
        if engine is None
        else []
        if engine == "absent"
        else [table(engine, table_name="events_shard")]
    )
    if not storage or engine == "Distributed":
        with pytest.raises(ClickHouseClusterTopologyError, match="physical storage"):
            outage.inspect_outage_layout(session_for([table()], storage=storage), "db.events")
    else:
        result = outage.inspect_outage_layout(session_for([table()], storage=storage), "db.events")
        assert result.physical_table == "`db`.`events_shard`"
        assert result.owns_physical is True
        assert result.physical_ddl == storage[0].create_sql


def test_outage_layout_rejects_dynamic_reference_without_claiming_storage_ownership():
    dynamic = table(engine_full="Distributed('core', currentDatabase())")
    with pytest.raises(ClickHouseClusterTopologyError, match="Cannot verify managed"):
        outage.inspect_outage_layout(session_for([dynamic]), "db.events")


@pytest.mark.parametrize("pair", [False, True])
def test_unqualified_target_snapshot_keeps_verified_logical_and_physical_namespaces(pair):
    class Client:
        def query(self, sql):
            if sql == "SHOW CREATE TABLE events":
                engine = (
                    "Distributed('storage', 'other.db', 'events', id)"
                    if pair
                    else "MergeTree ORDER BY id"
                )
                ddl = f'CREATE TABLE "current.db".events (id Int64) ENGINE = {engine}'
            elif "create_table_query" in sql:
                ddl = 'CREATE TABLE "other.db".events (id Int64) ENGINE = MergeTree ORDER BY id'
            else:
                pytest.fail(f"Unexpected metadata query: {sql}")
            return SimpleNamespace(result_rows=[(ddl,)])

    snapshot = layout.inspect_existing_layout(Client(), "events")
    logical = parse_one(snapshot.logical_table, read="clickhouse", into=exp.Table)
    physical = parse_one(snapshot.physical_table, read="clickhouse", into=exp.Table)
    assert (logical.db, logical.name) == ("current.db", "events")
    assert (physical.db, physical.name) == ("other.db" if pair else "current.db", "events")
    assert snapshot.shard_on_cluster == ("storage" if pair else None)


def test_outage_unqualified_local_snapshot_uses_exact_current_database_identity():
    snapshot = outage.inspect_outage_layout(
        session_for([table("MergeTree")], database="current.db"), "events"
    )
    logical = parse_one(snapshot.logical_table, read="clickhouse", into=exp.Table)
    physical = parse_one(snapshot.physical_table, read="clickhouse", into=exp.Table)
    assert (logical.db, logical.name) == ("current.db", "events")
    assert (physical.db, physical.name) == ("current.db", "events")
    assert snapshot.shard_on_cluster is None
