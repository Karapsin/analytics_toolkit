"""Exact create-target presence uses the current namespace and actual client scope."""

from __future__ import annotations

import warnings
from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.outage_context import internal_io
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp

from tests.sql._support.ch_execute_create_scope import install_execute_create_scope

_TARGET = "analytics.remote_presence"


def _install(monkeypatch, write_sql_connections, *, bare=False):
    def write_native(connections):
        configured = deepcopy(connections)
        configured["ch"].update(driver="native", port=9000)
        return write_sql_connections(configured)

    state = install_execute_create_scope(monkeypatch, write_native, local_policy=True)
    if bare:
        open_connection = state.open

        def open_raw(config, **kwargs):
            with internal_io():
                return open_connection(config, **kwargs)

        monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", open_raw)
    return state


def _assert_unchanged_and_closed(state, before):
    assert state.snapshot() == before
    assert state.commands == []
    assert all(commands == [] for commands in state.host_commands.values())
    assert state.opened
    assert all(client.closed for client in state.opened)
    assert all(client.close_calls == 1 for client in state.opened)


def _seed_remote(state):
    create = parse_create_table(
        f"CREATE TABLE {_TARGET} (id UInt64) ENGINE=MergeTree ORDER BY id", _TARGET
    )
    state._create(state.remote, create, state.foreign_rows)
    assert _TARGET not in state.host_tables[state.coordinator]


def _skip_unqualified(*, policy="stop"):
    return sql.execute_create(
        "ch",
        "remote_presence",
        "CREATE TABLE analytics.unused_setup (id UInt64) ENGINE=MergeTree; SELECT 201 AS id",
        if_not_exists=True,
        ch_distributed_table=False,
        ch_engine="MergeTree",
        ch_shard_on_cluster="storage",
        ch_unavailable_shards=policy,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )


def test_execute_create_new_local_plain_target_preserves_other_endpoint_resources(
    monkeypatch, write_sql_connections
):
    state = _install(monkeypatch, write_sql_connections)
    target = "analytics.new_local_presence"
    before = state.snapshot()

    result = sql.execute_create(
        "ch",
        target,
        "SELECT 201 AS id",
        ch_distributed_table=False,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows == 1
    assert result.metadata.statement_count == 1
    assert state.rows(state.coordinator, target) == [(201,)]
    assert target not in state.host_tables[state.remote]
    after = state.snapshot()
    assert after[state.remote] == before[state.remote]
    unchanged = {name: value for name, value in after[state.coordinator].items() if name != target}
    assert unchanged == before[state.coordinator]
    assert len(state.commands) == 1
    assert state.opened
    assert all(client.closed for client in state.opened)
    assert all(client.close_calls == 1 for client in state.opened)


@pytest.mark.parametrize(
    ("bare", "policy"), [(False, "stop"), (True, "stop"), (False, "healthy_shards")]
)
def test_execute_create_unqualified_remote_skip_uses_actual_current_database(
    monkeypatch, write_sql_connections, bare, policy
):
    state = _install(monkeypatch, write_sql_connections, bare=bare)
    _seed_remote(state)
    before = state.snapshot()

    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always")
        result = _skip_unqualified(policy=policy)

    assert result.rows == 0
    assert result.metadata.statement_count == 0
    assert result.metadata.inserted_rows == 0
    assert result.metadata.affected_rows == 0
    assert result.metadata.data_complete is True
    assert not any(
        issubclass(item.category, sql.ClickHouseDegradedClusterWarning) for item in observed
    )
    assert state.rows(state.remote, _TARGET) == state.foreign_rows
    assert any("currentDatabase()" in statement for statement in state.queries)
    _assert_unchanged_and_closed(state, before)


@pytest.mark.parametrize(
    "rows", [[], [(None,)], [("analytics", "extra")], [("analytics",), ("analytics",)]]
)
def test_execute_create_unqualified_target_rejects_unverifiable_current_database_before_ddl(
    monkeypatch, write_sql_connections, rows
):
    state = _install(monkeypatch, write_sql_connections)
    _seed_remote(state)
    before = state.snapshot()

    def metadata_response(tree, _actual_rows):
        if tree.args.get("from_") is None and len(tree.expressions) == 1:
            function = tree.expressions[0]
            if isinstance(function, exp.Func):
                name = function.name or function.sql_name()
                if name.replace("_", "").casefold() == "currentdatabase":
                    return rows
        return None

    state.metadata_transform = metadata_response

    with pytest.raises(InvalidSqlInputError, match="Cannot verify the current database"):
        _skip_unqualified()

    assert state.metadata_faults
    _assert_unchanged_and_closed(state, before)
