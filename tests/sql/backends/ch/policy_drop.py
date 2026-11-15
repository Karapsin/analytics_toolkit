"""Policy-target drops retain exact certified resource scopes through recovery."""

from __future__ import annotations

from copy import deepcopy
from itertools import count
from types import SimpleNamespace

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch import drop_execution
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.policy_drop import execute_policy_target_drop
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import install_drop_scope

_TARGET = "analytics.events"
_PHYSICAL = "analytics.events_shard"


@pytest.fixture
def drop_scope_context(monkeypatch, write_sql_connections, request):
    options = getattr(request, "param", {"driver": "http"})
    state = install_drop_scope(monkeypatch, write_sql_connections, **options)
    connection = sql.get_sql_connection(options.get("connection_key", "ch"))
    try:
        yield state, connection
    finally:
        connection.close()
        assert state.opened
        assert all(client.closed for client in state.opened)
        assert all(client.close_calls == 1 for client in state.opened)


def _drop_resources(statements):
    resources = []
    for statement in statements:
        tree = parse_one(strip_query_label(statement), read=ClickHouseDialect)
        assert isinstance(tree, exp.Drop)
        cluster = tree.find(exp.OnCluster)
        scope = cluster.this.name if cluster is not None else None
        resources.extend(
            (table.db, table.name, scope) for table in tree.args.get("tables") or [tree.this]
        )
    return resources


def _metadata_with_structured_names(state, monkeypatch):
    metadata = state._metadata

    def exact_metadata(name, hosts):
        records = metadata(name, hosts)
        if name not in {"system.tables", "system.columns"}:
            return records
        for record in records:
            host = next(
                host for host in hosts if state.server_identities[host][0] == record["serverUUID"]
            )
            relation = record["table"] if name == "system.columns" else record["name"]
            value = state.host_tables[host][f"{record['database']}.{relation}"]
            target = value.create.this.this
            record["database"] = target.db
            record["table" if name == "system.columns" else "name"] = target.name
        return records

    monkeypatch.setattr(state, "_metadata", exact_metadata)


def _advance_wait_clock(monkeypatch):
    ticks = count(step=301)
    monkeypatch.setattr(
        drop_execution,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _seconds: None),
    )


def _hold_local_drop(monkeypatch, state, table):
    raw = state.opened[0]
    command = raw.command
    held = []

    def pending_command(statement, settings=None):
        tree = parse_one(strip_query_label(statement), read=ClickHouseDialect)
        if isinstance(tree, exp.Drop) and tree.find(exp.OnCluster) is None:
            targets = tree.args.get("tables") or [tree.this]
            assert len(targets) == 1
            assert (targets[0].db, targets[0].name) == tuple(table.split(".", 1))
            state.commands.append(statement)
            state.command_settings.append(deepcopy(settings))
            state.host_commands[raw.host].append(statement)
            held.append(statement)
            return None
        return command(statement, settings=settings)

    monkeypatch.setattr(raw, "command", pending_command)
    return held


def test_drop_keeps_exact_resource_identity_and_independent_execution_scopes(
    drop_scope_context, monkeypatch
):
    state, connection = drop_scope_context
    state.clusters["facades"] = (state.remote,)
    for host, relation, rows in [
        (state.coordinator, "events", [(1,)]),
        (state.remote, "events", [(2,)]),
        (state.remote, "physical", [(3,)]),
    ]:
        name = f"`analytics.db`.`{relation}`"
        create = parse_create_table(
            f"CREATE TABLE {name} (id UInt64) ENGINE=MergeTree ORDER BY id", name
        )
        state._create(host, create, rows)
        # Preserve the parsed components when the general fixture retargets its opaque key.
        state.host_tables[host][f"analytics.db.{relation}"].create = create.copy()
    _metadata_with_structured_names(state, monkeypatch)
    before = state.snapshot()
    expected = deepcopy(before)
    expected[state.coordinator].pop("analytics.db.events")
    expected[state.remote].pop("analytics.db.events")
    expected[state.remote].pop("analytics.db.physical")
    statements = [
        "DROP TABLE IF EXISTS `analytics.db`.`events`",
        "DROP TABLE IF EXISTS `analytics.db`.`events` ON CLUSTER facades",
        "DROP TABLE IF EXISTS `analytics.db`.`physical` ON CLUSTER storage",
    ]

    result = execute_policy_target_drop(
        get_backend_adapter("ch"), connection, statements, target_key="ch"
    )

    assert result is None
    assert state.snapshot() == expected
    assert _drop_resources(state.commands) == [
        ("analytics.db", "events", None),
        ("analytics.db", "events", "facades"),
        ("analytics.db", "physical", "storage"),
    ]
    assert _drop_resources(state.host_commands[state.coordinator]) == [
        ("analytics.db", "events", None)
    ]
    assert _drop_resources(state.host_commands[state.remote]) == [
        ("analytics.db", "events", "facades"),
        ("analytics.db", "physical", "storage"),
    ]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]


@pytest.mark.parametrize("scope", [None, "storage"])
def test_absence_timeout_preserves_failure_when_host_retry_is_disabled_or_unavailable(
    drop_scope_context, monkeypatch, scope
):
    state, connection = drop_scope_context
    _advance_wait_clock(monkeypatch)
    table = _PHYSICAL if scope is not None else _TARGET
    held = []
    if scope is None:
        held = _hold_local_drop(monkeypatch, state, table)
    else:
        state.delay_cluster_drops = {(scope, table)}
    before = state.snapshot()
    suffix = f" ON CLUSTER {scope}" if scope is not None else ""

    with pytest.raises(TimeoutError, match=r"remains visible.*300 second") as caught:
        execute_policy_target_drop(
            get_backend_adapter("ch"),
            connection,
            [f"DROP TABLE IF EXISTS {table}{suffix}"],
            target_key="ch",
            retry_per_host_drops=scope is None,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert state.snapshot() == before
    assert len(state.opened) == 1
    assert _drop_resources(state.commands) == [("analytics", table.split(".")[1], scope)]
    if scope is None:
        assert held == state.commands
    else:
        assert state.delayed_cluster_drops == [(scope, table)]


@pytest.mark.parametrize(
    "drop_scope_context", [{"driver": "native", "connection_key": "alias"}], indirect=True
)
def test_host_recovery_never_invents_companion_names_and_keeps_native_port(
    drop_scope_context, monkeypatch
):
    state, connection = drop_scope_context
    _advance_wait_clock(monkeypatch)
    name, address, _port = state.host_endpoints[state.remote]
    state.host_endpoints[state.remote] = (name, address, 9441)
    server_uuid, _port = state.server_identities[state.remote]
    state.server_identities[state.remote] = (server_uuid, 9441)
    state.delay_cluster_drops = {("storage", _PHYSICAL)}
    before = state.snapshot()
    expected = deepcopy(before)
    expected[state.remote].pop(_PHYSICAL)

    result = execute_policy_target_drop(
        get_backend_adapter("ch"),
        connection,
        [f"DROP TABLE IF EXISTS {_PHYSICAL} ON CLUSTER storage"],
        target_key="alias",
    )

    assert result is None
    assert state.snapshot() == expected
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert _drop_resources(state.commands) == [
        ("analytics", "events_shard", "storage"),
        ("analytics", "events_shard", None),
    ]
    assert state.host_commands[state.coordinator] == []
    assert state.delayed_cluster_drops == [("storage", _PHYSICAL)]
    assert [
        (config.host, config.port, config.driver)
        for config in state.opened_configs
        if config.host == address
    ] == [(address, 9441, "native")]
    assert len(state.opened) == 2
    assert all(config.connection_key == "alias" for config in state.opened_configs)
    assert state.opened[1].closed
    assert state.opened[1].close_calls == 1


@pytest.mark.parametrize(
    "statement", ["SELECT 1", "DROP TABLE db.a, db.b", "DROP TABLE db.a ON CLUSTER {name:String}"]
)
def test_unverifiable_drop_batches_fail_before_any_submission(drop_scope_context, statement):
    state, connection = drop_scope_context
    before = state.snapshot()
    queries = list(state.queries)

    with pytest.raises(InvalidSqlInputError):
        execute_policy_target_drop(
            get_backend_adapter("ch"),
            connection,
            [f"DROP TABLE IF EXISTS {_TARGET}", statement],
            target_key="ch",
        )

    assert state.snapshot() == before
    assert state.commands == []
    assert state.queries == queries


def test_empty_drop_batch_has_no_backend_side_effects(drop_scope_context):
    state, connection = drop_scope_context
    before = state.snapshot()
    queries = list(state.queries)

    result = execute_policy_target_drop(get_backend_adapter("ch"), connection, [], target_key="ch")

    assert result is None
    assert state.snapshot() == before
    assert state.commands == []
    assert state.queries == queries
