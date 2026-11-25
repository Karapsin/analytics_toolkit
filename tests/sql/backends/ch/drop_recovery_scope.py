"""Delayed drop recovery and identity rechecks retain exact resource ownership."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import install_drop_scope

_TARGET = "analytics.events"
_PHYSICAL = "analytics.events_shard"


def _drop(*, wait):
    return sql.drop_tables(
        "ch",
        _TARGET,
        ch_cluster="storage",
        if_exists=True,
        ch_wait_for_absence=wait,
        ch_wait_timeout_seconds=1,
        ch_wait_poll_interval_seconds=0.01,
        ch_retry_per_host_drops=True,
    )


def _drop_resources(statements):
    resources = []
    for statement in statements:
        tree = parse_one(strip_query_label(statement), read=ClickHouseDialect)
        assert isinstance(tree, exp.Drop)
        cluster = tree.find(exp.OnCluster)
        scope = cluster.this.name if cluster is not None else None
        resources.extend(
            (f"{table.db}.{table.name}", scope) for table in tree.args.get("tables") or [tree.this]
        )
    return resources


def _assert_closed_once(state):
    assert state.opened
    assert all(client.closed for client in state.opened)
    assert all(client.close_calls == 1 for client in state.opened)


def test_delayed_cluster_drop_recovers_only_physical_role_at_its_native_port(
    monkeypatch, write_sql_connections
):
    state = install_drop_scope(monkeypatch, write_sql_connections, driver="native")
    name, address, _port = state.host_endpoints[state.remote]
    state.host_endpoints[state.remote] = (name, address, 9441)
    server_uuid, _port = state.server_identities[state.remote]
    state.server_identities[state.remote] = (server_uuid, 9441)
    foreign = parse_create_table(
        f"CREATE TABLE {_TARGET} (id UInt64) ENGINE=MergeTree ORDER BY id", _TARGET
    )
    state._create(state.remote, foreign, state.foreign_rows)
    state.delay_cluster_drops = {("storage", _PHYSICAL)}
    before = state.snapshot()
    expected = deepcopy(before)
    expected[state.coordinator].pop(_TARGET)
    expected[state.remote].pop(_PHYSICAL)

    result = _drop(wait=True)

    after = state.snapshot()
    assert result is None
    assert after == expected, repr({"before": before, "after": after, "commands": state.commands})
    assert after[state.coordinator][_PHYSICAL] == before[state.coordinator][_PHYSICAL]
    assert after[state.remote][_TARGET] == before[state.remote][_TARGET]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.remote, _TARGET) == state.foreign_rows
    assert len(state.foreign_rows) == 100
    assert state.delay_cluster_drops == set()
    assert state.delayed_cluster_drops == [("storage", _PHYSICAL)]
    assert _drop_resources(state.commands) == [
        (_TARGET, None),
        (_PHYSICAL, "storage"),
        (_PHYSICAL, None),
    ]
    assert _drop_resources(state.host_commands[state.coordinator]) == [(_TARGET, None)]
    assert _drop_resources(state.host_commands[state.remote]) == [
        (_PHYSICAL, "storage"),
        (_PHYSICAL, None),
    ]
    assert [
        (config.host, config.port, config.driver)
        for config in state.opened_configs
        if config.host == address
    ] == [(address, 9441, "native")]
    _assert_closed_once(state)


def _physical_uuid_query(statement):
    tree = parse_one(strip_query_label(statement), read=ClickHouseDialect)
    if not isinstance(tree, exp.Select):
        return False
    if [item.name for item in tree.expressions] != ["_shard_num", "uuid"]:
        return False
    source = tree.args.get("from_")
    if (
        source is None
        or not isinstance(source.this, exp.Table)
        or not isinstance(source.this.this, exp.Anonymous)
    ):
        return False
    cluster, database, relation, _trailing = cluster_arguments(source.this.this, "analytics")
    return (cluster, database, relation) == ("storage", "system", "tables") and any(
        isinstance(condition.this, exp.Column)
        and condition.this.name == "name"
        and isinstance(condition.expression, exp.Literal)
        and condition.expression.this == "events_shard"
        for condition in tree.find_all(exp.EQ)
    )


def test_same_definition_new_table_uuid_is_rejected_before_any_drop(
    monkeypatch, write_sql_connections
):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    before = state.snapshot()
    replacements = []

    def replace_after_identity_read(connection, statement, result):
        if (
            replacements
            or connection.host != state.coordinator
            or not _physical_uuid_query(statement)
        ):
            return
        old = state.host_tables[state.remote].pop(_PHYSICAL)
        assert result.result_rows == [(1, old.uuid)]
        state._create(state.remote, old.create.copy(), deepcopy(old.rows))
        replacements.append(state.snapshot())

    state.after_query = replace_after_identity_read

    with pytest.raises(InvalidSqlInputError, match="changed after drop ownership was verified"):
        _drop(wait=False)

    assert len(replacements) == 1
    after = state.snapshot()
    assert after == replacements[0]
    old = before[state.remote][_PHYSICAL]
    replacement = after[state.remote][_PHYSICAL]
    assert replacement["uuid"] != old["uuid"]
    assert {key: value for key, value in replacement.items() if key != "uuid"} == {
        key: value for key, value in old.items() if key != "uuid"
    }
    expected = deepcopy(before)
    expected[state.remote][_PHYSICAL] = replacement
    assert after == expected
    assert state.commands == []
    assert all(commands == [] for commands in state.host_commands.values())
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]
    _assert_closed_once(state)
