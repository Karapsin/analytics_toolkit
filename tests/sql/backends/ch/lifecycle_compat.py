"""Retained host-runner helpers operate only on explicitly supplied owned pairs."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch import lifecycle
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.backends.ch.recovery_endpoints import RecoveryEndpoint
from analytics_toolkit.sql.connection.get_sql_connection import get_ch_connection_for_host
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import install_drop_scope

_PAIR = lifecycle.ChDistributedTablePair("analytics.compat_events", "analytics.compat_storage")


@pytest.fixture
def lifecycle_context(monkeypatch, write_sql_connections):
    state = install_drop_scope(monkeypatch, write_sql_connections, driver="native")
    name, address, _port = state.host_endpoints[state.remote]
    state.host_endpoints[state.remote] = (name, address, 9441)
    identity, _port = state.server_identities[state.remote]
    state.server_identities[state.remote] = (identity, 9441)
    for table, rows in [(_PAIR.distributed_table, [(21,)]), (_PAIR.shard_table, [(22,)])]:
        create = parse_create_table(
            f"CREATE TABLE {table} (id UInt64) ENGINE=MergeTree ORDER BY id", table
        )
        state._create(state.remote, create, rows)
    connection = sql.get_sql_connection("ch")
    try:
        yield state, connection
    finally:
        connection.close()
        assert state.opened
        assert all(client.closed for client in state.opened)
        assert all(client.close_calls == 1 for client in state.opened)


def _drops(statements):
    resources = []
    for statement in statements:
        tree = parse_one(strip_query_label(statement), read=ClickHouseDialect)
        assert isinstance(tree, exp.Drop)
        assert tree.find(exp.OnCluster) is None
        resources.extend(
            f"{table.db}.{table.name}" for table in tree.args.get("tables") or [tree.this]
        )
    return resources


def _assert_owned_pair_removed(state, before):
    expected = deepcopy(before)
    owned = [
        expected[state.remote].pop(table) for table in (_PAIR.distributed_table, _PAIR.shard_table)
    ]
    assert all(value["uuid"] for value in owned)
    assert state.snapshot() == expected
    assert _drops(state.commands) == [_PAIR.distributed_table, _PAIR.shard_table]
    assert state.host_commands[state.coordinator] == []
    assert state.rows(state.coordinator, "analytics.events_shard") == state.foreign_rows
    assert state.rows(state.remote, "analytics.events_shard") == [(11,), (12,), (13,)]
    address = state.host_endpoints[state.remote][1]
    assert [
        (config.host, config.port)
        for config in state.opened_configs
        if config.host in {state.remote, address}
    ] == [(state.remote, 9441)]
    assert len(state.opened) == 2
    assert state.opened[1].closed
    assert state.opened[1].close_calls == 1


def test_bounded_compat_runner_resolves_live_endpoint_and_drops_only_owned_pair(
    lifecycle_context,
):
    state, connection = lifecycle_context
    before = state.snapshot()
    phases = []
    acquired = []

    def connection_runner(phase, operation):
        phases.append(phase)
        return operation(connection)

    def host_runner(endpoint, operation):
        acquired.append(endpoint)
        worker = get_ch_connection_for_host("ch", endpoint)
        try:
            return operation(worker)
        finally:
            worker.close()

    hosts = lifecycle._resolve_bounded_drop_hosts(connection_runner, _PAIR, "storage")
    assert phases == ["resolve_target_drop_hosts"]
    assert [(str(host), host.tcp_port) for host in hosts] == [(state.remote, 9441)]
    assert state.snapshot() == before
    assert state.commands == []

    result = lifecycle._drop_pair_on_bounded_hosts(hosts, _PAIR, None, host_runner, 2)

    assert result is None
    assert acquired == hosts
    _assert_owned_pair_removed(state, before)


@pytest.mark.parametrize("endpoint_kind", ["discovered", "bare-string"])
def test_bounded_compat_runner_reports_native_endpoint_and_original_open_error(
    lifecycle_context, endpoint_kind
):
    state, connection = lifecycle_context
    before = state.snapshot()

    def connection_runner(_phase, operation):
        return operation(connection)

    discovered = lifecycle._resolve_bounded_drop_hosts(connection_runner, _PAIR, "storage")
    assert [(str(host), host.tcp_port) for host in discovered] == [(state.remote, 9441)]
    hosts = discovered if endpoint_kind == "discovered" else [str(host) for host in discovered]
    attempted = []
    original_error = ConnectionError("native recovery connection open failed")

    def failing_host_runner(endpoint, _operation):
        attempted.append(endpoint)
        raise original_error

    with pytest.raises(TimeoutError) as caught:
        lifecycle._drop_pair_on_bounded_hosts(hosts, _PAIR, None, failing_host_runner, 2)

    expected_endpoint = discovered[0].identity if endpoint_kind == "discovered" else state.remote
    diagnostic = str(caught.value)
    assert expected_endpoint in diagnostic
    assert repr(original_error) in diagnostic
    assert attempted == hosts
    assert state.snapshot() == before
    assert state.commands == []
    assert all(commands == [] for commands in state.host_commands.values())
    assert len(state.opened) == 1


def test_legacy_cluster_host_cleanup_preserves_native_endpoint_and_success_return(
    lifecycle_context,
):
    state, connection = lifecycle_context
    before = state.snapshot()
    acquired = []

    def open_host(endpoint):
        acquired.append(endpoint)
        return get_ch_connection_for_host("ch", endpoint)

    result = lifecycle._drop_ch_distributed_table_pair_on_cluster_hosts(
        connection,
        _PAIR,
        ch_cluster="storage",
        query_label=None,
        per_host_drop_workers=2,
        per_host_connection_factory=open_host,
    )

    assert result is None
    assert [(str(host), host.tcp_port) for host in acquired] == [(state.remote, 9441)]
    _assert_owned_pair_removed(state, before)


@pytest.mark.parametrize(
    "condition", ["metadata-unavailable", "already-absent", "membership-drift"]
)
def test_legacy_host_selection_keeps_configured_snapshot_when_leftovers_are_inconclusive(
    lifecycle_context, condition
):
    state, connection = lifecycle_context
    configured = [RecoveryEndpoint(state.remote, 9441)]
    faults = []
    if condition == "metadata-unavailable":

        def fail_leftovers(_connection, statement):
            prefix = "SELECT hostName(), database, name, engine"
            if strip_query_label(statement).startswith(prefix):
                faults.append(statement)
                message = "distributed metadata read unavailable"
                raise RuntimeError(message)

        state.before_query = fail_leftovers
    elif condition == "already-absent":
        state.host_tables[state.remote].pop(_PAIR.distributed_table)
        state.host_tables[state.remote].pop(_PAIR.shard_table)
    else:
        configured = [RecoveryEndpoint("previous-member", 9551)]
    before = state.snapshot()

    selected = lifecycle._select_ch_hosts_for_local_drop(
        connection, _PAIR, ch_cluster="storage", configured_hosts=configured
    )

    assert selected == configured
    assert selected is not configured
    assert selected[0].tcp_port == configured[0].tcp_port
    assert state.snapshot() == before
    assert state.commands == []
    assert len(state.opened) == 1
    if condition == "metadata-unavailable":
        assert len(faults) == 1
