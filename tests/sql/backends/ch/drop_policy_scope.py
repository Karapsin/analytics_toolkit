"""Policy cleanup preserves storage outside each verified resource's DDL scope."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.creation_policy import (
    resolve_clickhouse_creation_policy,
)
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.layout import inspect_existing_layout
from analytics_toolkit.sql.backends.ch.lifecycle import (
    build_drop_ch_creation_policy_table_sqls,
    drop_ch_creation_policy_tables,
)
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import install_drop_scope

_TARGET = "analytics.events"
_PHYSICAL = "analytics.events_shard"
_POLICY_TARGET = "analytics.policy_events"


@pytest.fixture
def policy_scope_context(monkeypatch, write_sql_connections):
    state = install_drop_scope(monkeypatch, write_sql_connections)
    connection = sql.get_sql_connection("ch")
    try:
        yield state, connection
    finally:
        connection.close()
        assert state.opened
        assert all(client.closed for client in state.opened)
        assert all(client.close_calls == 1 for client in state.opened)


def _drops(statements, *, if_exists):
    resources = []
    assert isinstance(statements, list)
    for statement in statements:
        assert isinstance(statement, str)
        tree = parse_one(strip_query_label(statement), read=ClickHouseDialect)
        assert isinstance(tree, exp.Drop)
        assert bool(tree.args.get("exists")) is if_exists
        cluster = tree.find(exp.OnCluster)
        scope = cluster.this.name if cluster is not None else None
        resources.extend(
            (f"{table.db}.{table.name}", scope) for table in tree.args.get("tables") or [tree.this]
        )
    return resources


def _verified_layout(connection):
    layout = inspect_existing_layout(connection, _TARGET)
    assert layout.create_distributed_pair
    assert layout.owns_physical
    assert layout.shard_on_cluster == "storage"
    assert layout.distributed_on_cluster is None
    assert layout.distributed_cluster == "storage"
    return layout


def _plain_policy(scope):
    return resolve_clickhouse_creation_policy(
        get_connection_config("ch").ddl_defaults.regular,
        ch_engine="MergeTree",
        ch_cluster=None,
        ch_sharding_key=None,
        ch_distributed_table=False,
        ch_only_shard=False,
        ch_distributed_engine_template=None,
        ch_distributed_cluster=None,
        ch_shard_on_cluster=scope,
    )


@pytest.mark.parametrize("if_exists", [True, False])
def test_verified_layout_drop_sql_has_only_each_roles_actual_scope(policy_scope_context, if_exists):
    state, connection = policy_scope_context
    before = state.snapshot()
    layout = _verified_layout(connection)

    statements = build_drop_ch_creation_policy_table_sqls(_TARGET, layout, if_exists=if_exists)

    assert _drops(statements, if_exists=if_exists) == [
        (_TARGET, None),
        (_PHYSICAL, "storage"),
    ]
    assert state.snapshot() == before
    assert state.commands == []
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows


@pytest.mark.parametrize("if_exists", [True, False])
def test_verified_layout_cleanup_preserves_foreign_coordinator_physical_table(
    policy_scope_context, if_exists
):
    state, connection = policy_scope_context
    before = state.snapshot()
    layout = _verified_layout(connection)
    expected = deepcopy(before)
    expected[state.coordinator].pop(_TARGET)
    expected[state.remote].pop(_PHYSICAL)

    result = drop_ch_creation_policy_tables(connection, _TARGET, layout, if_exists=if_exists)

    after = state.snapshot()
    assert after == expected, repr({"before": before, "after": after, "commands": state.commands})
    assert result is None
    assert after[state.coordinator][_PHYSICAL] == before[state.coordinator][_PHYSICAL]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert len(state.foreign_rows) == 100
    assert _drops(state.commands, if_exists=if_exists) == [
        (_TARGET, None),
        (_PHYSICAL, "storage"),
    ]
    assert _drops(state.host_commands[state.coordinator], if_exists=if_exists) == [(_TARGET, None)]
    assert _drops(state.host_commands[state.remote], if_exists=if_exists) == [
        (_PHYSICAL, "storage")
    ]


def test_desired_pair_cleanup_cannot_authorize_a_foreign_physical_scope(
    policy_scope_context,
):
    state, connection = policy_scope_context
    before = state.snapshot()
    layout = _verified_layout(connection)
    desired = resolve_clickhouse_creation_policy(
        get_connection_config("ch").ddl_defaults.regular,
        ch_engine="MergeTree",
        ch_cluster=None,
        ch_sharding_key="rand()",
        ch_distributed_table=True,
        ch_only_shard=False,
        ch_distributed_engine_template=None,
        ch_distributed_cluster="coordinator_only",
        ch_shard_on_cluster="coordinator_only",
        ch_distributed_on_cluster=None,
    )
    assert desired.create_distributed_pair
    assert desired.shard_on_cluster != layout.shard_on_cluster
    assert desired.distributed_cluster != layout.distributed_cluster
    error = None

    try:
        drop_ch_creation_policy_tables(connection, _TARGET, desired)
    except InvalidSqlInputError as exc:
        error = exc

    after = state.snapshot()
    assert after == before, repr({"before": before, "after": after, "commands": state.commands})
    assert isinstance(error, InvalidSqlInputError)
    assert state.commands == []
    assert all(commands == [] for commands in state.host_commands.values())
    assert after[state.coordinator][_PHYSICAL] == before[state.coordinator][_PHYSICAL]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert len(state.foreign_rows) == 100
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]


@pytest.mark.parametrize(
    ("scope", "owner", "foreign", "if_exists"),
    [
        ("storage", "remote", "coordinator", True),
        ("storage", "remote", "coordinator", False),
        ("coordinator_only", "coordinator", "remote", False),
    ],
    ids=["remote-placement", "remote-placement-required", "coordinator-member-required"],
)
def test_resolved_creation_policy_cleanup_drops_placement_once_without_local_mirror(
    policy_scope_context, scope, owner, foreign, if_exists
):
    state, connection = policy_scope_context
    definition = parse_create_table(
        f"CREATE TABLE {_POLICY_TARGET} (id UInt64) ENGINE=MergeTree ORDER BY id",
        _POLICY_TARGET,
    )
    state._create(owner, definition, [(7,), (8,)])
    state._create(foreign, definition, state.foreign_rows)
    before = state.snapshot()
    expected = deepcopy(before)
    expected[owner].pop(_POLICY_TARGET)
    policy = _plain_policy(scope)
    assert not policy.create_distributed_pair
    assert policy.shard_on_cluster == scope
    statements = build_drop_ch_creation_policy_table_sqls(
        _POLICY_TARGET, policy, if_exists=if_exists
    )
    assert state.commands == []
    assert state.snapshot() == before

    result = drop_ch_creation_policy_tables(connection, _POLICY_TARGET, policy, if_exists=if_exists)

    after = state.snapshot()
    assert after == expected, repr({"before": before, "after": after, "commands": state.commands})
    assert result is None
    assert after[foreign][_POLICY_TARGET] == before[foreign][_POLICY_TARGET]
    assert state.rows(foreign, _POLICY_TARGET) == state.foreign_rows
    assert _drops(statements, if_exists=if_exists) == [(_POLICY_TARGET, scope)]
    assert _drops(state.commands, if_exists=if_exists) == [(_POLICY_TARGET, scope)]
    assert state.host_commands[foreign] == []
    assert _drops(state.host_commands[owner], if_exists=if_exists) == [(_POLICY_TARGET, scope)]
