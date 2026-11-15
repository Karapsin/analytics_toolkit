"""Public execute-create replacement respects independently placed storage."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.execution.plans import SqlPlan

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_execute_create_scope import install_execute_create_scope

_TARGET = "analytics.events"
_PHYSICAL = "analytics.events_shard"


def _execute_pair():
    return sql.execute_create(
        "ch",
        _TARGET,
        "SELECT 201 AS id",
        drop_if_exists=True,
        ch_distributed_table=True,
        ch_engine="MergeTree",
        ch_shard_on_cluster="storage",
        ch_distributed_cluster="storage",
        ch_distributed_on_cluster=None,
        retry_cnt=1,
        timeout_increment=0,
    )


def _assert_closed_once(state):
    assert state.opened
    assert all(client.closed for client in state.opened)
    assert all(client.close_calls == 1 for client in state.opened)


def test_execute_create_pair_replace_preserves_foreign_local_physical_table(
    monkeypatch, write_sql_connections
):
    state = install_execute_create_scope(monkeypatch, write_sql_connections)
    before = state.snapshot()
    foreign = deepcopy(before[state.coordinator][_PHYSICAL])
    assert len(foreign["rows"]) == 100
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]

    result = _execute_pair()

    assert isinstance(result, int)
    assert result == 1
    assert state.snapshot()[state.coordinator][_PHYSICAL] == foreign
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.remote, _PHYSICAL) == [(201,)]
    assert state.rows(state.coordinator, _TARGET) == [(201,)]
    _assert_closed_once(state)


def test_execute_create_pair_replace_same_placement_creates_and_populates_once(
    monkeypatch, write_sql_connections
):
    state = install_execute_create_scope(monkeypatch, write_sql_connections)
    state.host_tables[state.coordinator].pop(_PHYSICAL)
    before = state.snapshot()

    result = _execute_pair()

    assert result == 1
    assert state.rows(state.remote, _PHYSICAL) == [(201,)]
    assert state.rows(state.coordinator, _TARGET) == [(201,)]
    after = state.snapshot()
    assert after[state.remote][_PHYSICAL]["uuid"] != before[state.remote][_PHYSICAL]["uuid"]
    assert after[state.coordinator][_TARGET]["uuid"] != before[state.coordinator][_TARGET]["uuid"]
    _assert_closed_once(state)


@pytest.mark.parametrize("planning_option", ["dry_run", "return_sql"])
def test_execute_create_replace_plan_defers_drop_ownership_without_opening_connections(
    monkeypatch, write_sql_connections, planning_option
):
    state = install_execute_create_scope(monkeypatch, write_sql_connections)
    before = state.snapshot()

    plan = sql.execute_create(
        "ch",
        _TARGET,
        "SELECT 201 AS id",
        drop_if_exists=True,
        retry_cnt=1,
        timeout_increment=0,
        **{planning_option: True},
    )

    assert isinstance(plan, SqlPlan)
    assert plan.options["pending_existing_layout_verification"] is True
    assert plan.options["pending_resource_scope_resolution"] is True
    assert all(statement.phase != "drop_target" for statement in plan.statements)
    assert all(
        not strip_query_label(statement.sql).lstrip().upper().startswith("DROP ")
        for statement in plan.statements
    )
    assert state.snapshot() == before
    assert state.opened == []
    assert state.queries == []
    assert state.commands == []


@pytest.mark.parametrize("source", ["schema", "query"])
def test_destructive_only_sql_generation_requires_runtime_ownership_resolution(
    monkeypatch, write_sql_connections, source
):
    state = install_execute_create_scope(monkeypatch, write_sql_connections)
    source_options = (
        {"table_schema": {"id": "UInt64"}} if source == "schema" else {"sql": "SELECT 201 AS id"}
    )

    with pytest.raises(InvalidSqlInputError, match=r"(?i)(runtime|ownership|resolv|verif)"):
        sql.create_table(
            "ch", _TARGET, drop_if_exists=True, only_generate_sql=True, **source_options
        )

    if source == "schema":
        statement = sql.create_table("ch", _TARGET, only_generate_sql=True, **source_options)
        assert isinstance(statement, str)
        assert "CREATE TABLE" in statement
        assert "DROP TABLE" not in statement
    assert state.opened == []
    assert state.queries == []
    assert state.commands == []


def test_execute_create_if_not_exists_skips_setup_and_population(
    monkeypatch, write_sql_connections
):
    state = install_execute_create_scope(monkeypatch, write_sql_connections)
    before = state.snapshot()

    result = sql.execute_create(
        "ch",
        _TARGET,
        "CREATE TABLE analytics.setup (id UInt64) ENGINE=MergeTree; SELECT 201 AS id",
        if_not_exists=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result == 0
    assert state.snapshot() == before
    assert state.commands == []
    _assert_closed_once(state)


def test_execute_create_default_existing_target_error_preserves_every_resource(
    monkeypatch, write_sql_connections
):
    state = install_execute_create_scope(monkeypatch, write_sql_connections)
    before = state.snapshot()

    with pytest.raises((InvalidSqlInputError, RuntimeError), match=r"(?i)exist"):
        sql.execute_create("ch", _TARGET, "SELECT 201 AS id", retry_cnt=1, timeout_increment=0)

    assert state.snapshot() == before
    assert all(
        not strip_query_label(statement).lstrip().upper().startswith("DROP ")
        for statement in state.commands
    )
    _assert_closed_once(state)


def test_execute_create_local_policy_replaces_only_the_local_target(
    monkeypatch, write_sql_connections
):
    state = install_execute_create_scope(monkeypatch, write_sql_connections, local_policy=True)
    state.host_tables[state.coordinator].pop(_TARGET)
    create = parse_create_table(
        f"CREATE TABLE {_TARGET} (id UInt64) ENGINE=MergeTree ORDER BY id", _TARGET
    )
    state._create(state.coordinator, create, [(31,)])
    state._create(state.remote, create, state.foreign_rows)
    before = state.snapshot()

    result = sql.execute_create(
        "ch",
        _TARGET,
        "SELECT 201 AS id",
        drop_if_exists=True,
        ch_distributed_table=False,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert result == 1
    assert state.rows(state.coordinator, _TARGET) == [(201,)]
    after = state.snapshot()
    assert after[state.remote] == before[state.remote]
    assert after[state.coordinator][_PHYSICAL] == before[state.coordinator][_PHYSICAL]
    assert after[state.coordinator][_TARGET]["uuid"] != before[state.coordinator][_TARGET]["uuid"]
    _assert_closed_once(state)


def test_execute_create_rejects_desired_storage_scope_collision_before_ddl(
    monkeypatch, write_sql_connections
):
    state = install_execute_create_scope(monkeypatch, write_sql_connections)
    before = state.snapshot()
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows

    with pytest.raises(InvalidSqlInputError):
        sql.execute_create(
            "ch",
            _TARGET,
            "SELECT 201 AS id",
            drop_if_exists=True,
            ch_distributed_table=True,
            ch_engine="MergeTree",
            ch_shard_on_cluster="incoming",
            ch_distributed_cluster="incoming",
            ch_distributed_on_cluster=None,
            retry_cnt=1,
            timeout_increment=0,
        )

    assert state.snapshot() == before
    assert state.commands == []
    _assert_closed_once(state)


@pytest.mark.parametrize("placement", ["remote", "local", "routed"])
def test_execute_create_if_not_exists_resolves_plain_target_scope_before_setup(
    monkeypatch, write_sql_connections, placement
):
    def write_config(connections):
        configured = deepcopy(connections)
        if placement == "routed":
            configured["ch"]["cluster_routing"] = {"cluster": "storage"}
        return write_sql_connections(configured)

    state = install_execute_create_scope(monkeypatch, write_config, local_policy=True)
    target = "analytics.remote_only"
    create = parse_create_table(
        f"CREATE TABLE {target} (id UInt64) ENGINE=MergeTree ORDER BY id", target
    )
    host = state.coordinator if placement == "local" else state.remote
    other = state.remote if placement == "local" else state.coordinator
    state._create(host, create, state.foreign_rows)
    assert target not in state.host_tables[other]
    assert len(state.rows(host, target)) == 100
    before = state.snapshot()

    result = sql.execute_create(
        "ch",
        target,
        "CREATE TABLE analytics.remote_setup (id UInt64) ENGINE=MergeTree; SELECT 201 AS id",
        if_not_exists=True,
        ch_distributed_table=False,
        ch_engine="MergeTree",
        ch_shard_on_cluster=None if placement == "local" else "storage",
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    after = state.snapshot()
    diagnostic = {
        "before": before,
        "after": after,
        "commands": state.commands,
        "counts": (
            result.rows,
            result.metadata.statement_count,
            result.metadata.affected_rows,
            result.metadata.inserted_rows,
        ),
    }
    assert after == before, repr(diagnostic)
    assert result.rows == 0
    assert result.metadata.statement_count == 0
    assert result.metadata.affected_rows == 0
    assert result.metadata.inserted_rows == 0
    assert state.commands == []
    assert all(commands == [] for commands in state.host_commands.values())
    assert state.rows(host, target) == state.foreign_rows
    _assert_closed_once(state)


def _coordinator_target(monkeypatch, write_sql_connections, kind):
    state = install_execute_create_scope(monkeypatch, write_sql_connections, local_policy=True)
    target = _TARGET
    if kind == "plain":
        target = "analytics.coordinator_existing"
        create = parse_create_table(
            f"CREATE TABLE {target} (id UInt64) ENGINE=MergeTree ORDER BY id", target
        )
        state._create(state.coordinator, create, state.foreign_rows)
    assert target in state.host_tables[state.coordinator]
    assert target not in state.host_tables[state.remote]
    return state, target


def _execute_remote_plain(target, *, if_not_exists=False):
    return sql.execute_create(
        "ch",
        target,
        "CREATE TABLE analytics.remote_setup (id UInt64) ENGINE=MergeTree; SELECT 201 AS id",
        if_not_exists=if_not_exists,
        ch_distributed_table=False,
        ch_engine="MergeTree",
        ch_shard_on_cluster="storage",
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )


@pytest.mark.parametrize("kind", ["plain", "facade"])
def test_execute_create_if_not_exists_retains_existing_coordinator_target_before_setup(
    monkeypatch, write_sql_connections, kind
):
    state, target = _coordinator_target(monkeypatch, write_sql_connections, kind)
    before = state.snapshot()

    result = _execute_remote_plain(target, if_not_exists=True)

    assert state.snapshot() == before, repr(state.commands)
    assert result.rows == 0
    assert result.metadata.statement_count == 0
    assert result.metadata.affected_rows == 0
    assert result.metadata.inserted_rows == 0
    assert state.commands == []
    assert all(commands == [] for commands in state.host_commands.values())
    _assert_closed_once(state)


@pytest.mark.parametrize("kind", ["plain", "facade"])
def test_execute_create_default_rejects_existing_coordinator_target_before_remote_ddl(
    monkeypatch, write_sql_connections, kind
):
    state, target = _coordinator_target(monkeypatch, write_sql_connections, kind)
    before = state.snapshot()

    with pytest.raises(InvalidSqlInputError, match="already exists"):
        _execute_remote_plain(target)

    assert state.snapshot() == before
    assert state.commands == []
    assert all(commands == [] for commands in state.host_commands.values())
    _assert_closed_once(state)


def test_execute_create_if_not_exists_does_not_adopt_a_companion_only_name(
    monkeypatch, write_sql_connections
):
    state = install_execute_create_scope(monkeypatch, write_sql_connections, local_policy=True)
    target = "analytics.companion_only"
    companion = f"{target}_shard"
    create = parse_create_table(
        f"CREATE TABLE {companion} (id UInt64) ENGINE=MergeTree ORDER BY id", companion
    )
    state._create(state.coordinator, create, state.foreign_rows)
    before = state.snapshot()
    assert all(target not in tables for tables in state.host_tables.values())

    result = sql.execute_create(
        "ch",
        target,
        "SELECT 201 AS id",
        if_not_exists=True,
        ch_distributed_table=False,
        ch_engine="MergeTree",
        ch_shard_on_cluster="storage",
        retry_cnt=1,
        timeout_increment=0,
    )

    assert isinstance(result, int)
    assert state.commands
    assert state.rows(state.remote, target) == [(201,)]
    assert state.rows(state.coordinator, target) == []
    assert state.rows(state.coordinator, companion) == state.foreign_rows
    after = state.snapshot()
    for host, original in before.items():
        assert {name: table for name, table in after[host].items() if name != target} == original
    _assert_closed_once(state)
