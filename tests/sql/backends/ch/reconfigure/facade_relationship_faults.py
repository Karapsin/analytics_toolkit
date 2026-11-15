"""Facade ownership requires complete, bound metadata from every selected server."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp

from tests.sql._support.reconfigure_facade_faults import (
    assert_no_mutation,
    assert_rejected_unchanged,
    mutate_facade,
    prepare_fault,
    projection,
    query_identity,
)
from tests.sql._support.reconfigure_pair_scope import configure_pair_scope

_DEFINITIONS = ("_shard_num", "create_table_query")
_MACROS = ("_shard_num", "macro", "substitution")
_ROUTING = ("_shard_num", "cluster", "host_name", "host_address", "port")


def _macro_facades(raw):
    raw.node_macros[raw.coordinator]["route"] = "storage"
    raw.node_macros[raw.remote]["route"] = "storage_alias"
    raw.node_clusters[raw.remote]["storage_alias"] = (raw.remote,)
    for host in (raw.coordinator, raw.remote):
        facade = raw.host_tables[host][raw.source_table]
        facade.create = facade.create.copy()
        engine_property = facade.create.find(exp.EngineProperty)
        assert engine_property is not None
        engine = engine_property.this
        engine.set("expressions", [exp.Literal.string("{route}"), *engine.expressions[1:]])
        facade.route = ("{route}", "analytics.events_shard")


def _definition_fault(kind, rows):
    if kind == "coordinator-omitted":
        return rows[1:]
    if kind == "remote-omitted":
        return rows[:1]
    ddl = {
        "wrong-declared-name": (
            "CREATE TABLE analytics.other_events (id UInt64) "
            "ENGINE=Distributed('storage', 'analytics', 'events_shard', rand())"
        ),
        "missing-engine": "CREATE TABLE analytics.events (id UInt64)",
        "unverifiable-storage-expression": (
            "CREATE TABLE analytics.events (id UInt64) "
            "ENGINE=Distributed(concat('stor', 'age'), 'analytics', 'events_shard', rand())"
        ),
    }[kind]
    return [rows[0], (rows[1][0], ddl)]


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("coordinator-omitted", "coordinator logical table"),
        ("remote-omitted", "every original facade"),
        ("wrong-declared-name", "unowned logical table definition"),
        ("missing-engine", "unowned logical table definition"),
        ("unverifiable-storage-expression", "unowned logical table definition"),
    ],
)
def test_facade_relationship_rejects_incomplete_or_unbound_table_definitions(
    monkeypatch, write_sql_connections, fault, message
):
    def transform(select, rows):
        if (
            query_identity(select) == ("core", "system", "tables")
            and projection(select) == _DEFINITIONS
        ):
            return _definition_fault(fault, rows)
        return None

    raw = prepare_fault(monkeypatch, write_sql_connections, transform)
    before = raw.snapshot()

    with pytest.raises(InvalidSqlInputError, match=message):
        mutate_facade()

    assert_rejected_unchanged(raw, before)


@pytest.mark.parametrize("kind", ["plain-data", "different-default", "different-storage-table"])
def test_facade_relationship_does_not_adopt_a_foreign_existing_frontend(
    monkeypatch, write_sql_connections, kind
):
    raw = prepare_fault(monkeypatch, write_sql_connections, lambda *_args: None)
    if kind == "plain-data":
        ddl = "CREATE TABLE analytics.events (id UInt64) ENGINE=MergeTree ORDER BY id"
        rows = [(value,) for value in range(1001, 1101)]
    else:
        column = "id UInt64 DEFAULT 7" if kind == "different-default" else "id UInt64"
        table = "other_shard" if kind == "different-storage-table" else "events_shard"
        ddl = (
            f"CREATE TABLE analytics.events ({column}) "
            f"ENGINE=Distributed('storage', 'analytics', '{table}', rand())"
        )
        rows = []
        if kind == "different-storage-table":
            raw._create(
                raw.remote,
                parse_create_table(
                    "CREATE TABLE analytics.other_shard (id UInt64) ENGINE=MergeTree ORDER BY id",
                    "analytics.other_shard",
                ),
                [(91,), (92,)],
            )
    raw.host_tables[raw.remote].pop(raw.source_table)
    raw._create(raw.remote, parse_create_table(ddl, raw.source_table), rows)
    before = raw.snapshot()

    with pytest.raises(InvalidSqlInputError, match="unowned logical table definition"):
        mutate_facade()

    assert_no_mutation(raw, before)


def _macro_fault(kind, rows):
    if kind == "missing-remote":
        return [row for row in rows if row[0] == 1]
    if kind == "duplicate-binding":
        return [*rows, rows[-1]]
    changed = list(rows[-1])
    if kind == "truncated-binding":
        changed.pop()
    elif kind == "unprobed-server":
        changed[0] = 99
    elif kind == "invalid-server-index":
        changed[0] = "unknown"
    else:
        changed[2] = ""
    return [*rows[:-1], tuple(changed)]


@pytest.mark.parametrize(
    "fault",
    [
        "missing-remote",
        "duplicate-binding",
        "truncated-binding",
        "unprobed-server",
        "invalid-server-index",
        "empty-substitution",
    ],
)
def test_facade_relationship_requires_unique_complete_per_server_macro_bindings(
    monkeypatch, write_sql_connections, fault
):
    def transform(select, rows):
        if query_identity(select) == ("core", "system", "macros") and projection(select) == _MACROS:
            return _macro_fault(fault, rows)
        return None

    raw = prepare_fault(monkeypatch, write_sql_connections, transform)
    _macro_facades(raw)
    before = raw.snapshot()

    with pytest.raises(InvalidSqlInputError, match="routing macros"):
        mutate_facade()

    assert_rejected_unchanged(raw, before)


def _routing_fault(kind, rows):
    if kind == "missing-remote-definition":
        return [row for row in rows if row[0] == 1]
    changed = list(rows[-1])
    if kind == "truncated-endpoint":
        changed.pop()
    elif kind == "unprobed-server":
        changed[0] = 99
    elif kind == "invalid-native-port":
        changed[4] = "unknown"
    elif kind == "unusable-native-port":
        changed[4] = 0
    else:
        changed[4] = 9001
    return [*rows[:-1], tuple(changed)]


@pytest.mark.parametrize(
    "fault",
    [
        "missing-remote-definition",
        "truncated-endpoint",
        "unprobed-server",
        "invalid-native-port",
        "unusable-native-port",
        "different-native-instance",
    ],
)
def test_facade_relationship_requires_the_original_storage_endpoints_on_every_server(
    monkeypatch, write_sql_connections, fault
):
    def transform(select, rows):
        if (
            query_identity(select) == ("core", "system", "clusters")
            and projection(select) == _ROUTING
        ):
            return _routing_fault(fault, rows)
        return None

    raw = prepare_fault(monkeypatch, write_sql_connections, transform)
    before = raw.snapshot()

    with pytest.raises(InvalidSqlInputError, match=r"storage endpoints|routing endpoints"):
        mutate_facade()

    assert_rejected_unchanged(raw, before)


def test_facade_relationship_resolves_each_servers_macro_alias_to_the_same_storage(
    monkeypatch, write_sql_connections
):
    raw = prepare_fault(monkeypatch, write_sql_connections, lambda *_args: None)
    _macro_facades(raw)
    physical = raw.host_tables[raw.remote]["analytics.events_shard"]
    identity = physical.uuid, physical.create.sql(dialect=ClickHouseDialect)

    result = mutate_facade()

    assert result.data["cleanup_complete"] is True
    assert result.metadata.source_rows == result.metadata.final_target_rows == 3
    assert raw.host_tables[raw.remote]["analytics.events_shard"] is physical
    assert (physical.uuid, physical.create.sql(dialect=ClickHouseDialect)) == identity
    assert raw.rows(raw.remote, "analytics.events_shard") == [(1,), (2,), (3,)]
    assert raw.rows(raw.coordinator, raw.source_table) == [(1,), (2,), (3,)]
    assert raw.rows(raw.remote, raw.source_table) == [(1,), (2,), (3,)]
    assert raw.closed


def test_facade_relationship_local_source_scope_cannot_own_a_remote_physical_destination(
    monkeypatch, write_sql_connections
):
    raw = prepare_fault(monkeypatch, write_sql_connections, lambda *_args: None)
    raw.host_tables[raw.remote].pop(raw.source_table)
    raw._create(
        raw.remote,
        parse_create_table(
            "CREATE TABLE analytics.events (id UInt64) ENGINE=MergeTree ORDER BY id",
            raw.source_table,
        ),
        [(value,) for value in range(1001, 1101)],
    )

    configure_pair_scope(write_sql_connections, None)
    before = raw.snapshot()

    with pytest.raises(InvalidSqlInputError, match="unowned logical table"):
        sql.ch_reconfigure_table(
            "ch",
            raw.source_table,
            to_defaults=True,
            retry_cnt=1,
            timeout_increment=0,
        )

    assert_no_mutation(raw, before)
