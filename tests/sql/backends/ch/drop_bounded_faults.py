"""Bounded drop failures never borrow host capacity outside their selected policy."""

from __future__ import annotations

from copy import deepcopy
from itertools import count
from types import SimpleNamespace

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch import drop_execution
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.drop_bounded import execute_bounded_verified_drop
from analytics_toolkit.sql.backends.ch.drop_execution import DropExecutionOptions
from analytics_toolkit.sql.backends.ch.drop_layout import (
    DropIntent,
    VerifiedDropLayout,
    certify_existing_drop,
    certify_explicit_drop,
)
from analytics_toolkit.sql.backends.ch.lifecycle import drop_ch_distributed_table_pair_bounded
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError
from analytics_toolkit.sql.connection.get_sql_connection import get_ch_connection_for_host
from analytics_toolkit.sql.dml.transfer.runtime.connection_pool import BoundedConnectionManager
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import install_drop_scope

_TARGET = "analytics.events"
_PHYSICAL = "analytics.events_shard"


def _drop_resource(statement):
    tree = parse_one(strip_query_label(statement), read=ClickHouseDialect)
    assert isinstance(tree, exp.Drop)
    targets = tree.args.get("tables") or [tree.this]
    assert len(targets) == 1
    cluster = tree.find(exp.OnCluster)
    return (
        f"{targets[0].db}.{targets[0].name}",
        cluster.this.name if cluster is not None else None,
    )


def _delay_local_acknowledgement(monkeypatch, state, raw, held):
    command = raw.command

    def delayed(statement, settings=None):
        if _drop_resource(statement) == (_TARGET, None):
            state.commands.append(statement)
            state.command_settings.append(deepcopy(settings))
            state.host_commands[raw.host].append(statement)
            held.append(statement)
            return None
        return command(statement, settings=settings)

    monkeypatch.setattr(raw, "command", delayed)


def _assert_protected_state_and_closed_pool(state, before, expected, manager):
    assert state.snapshot() == expected
    assert state.snapshot()[state.coordinator][_PHYSICAL] == before[state.coordinator][_PHYSICAL]
    assert state.snapshot()[state.remote][_PHYSICAL] == before[state.remote][_PHYSICAL]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]
    assert manager.high_water_mark == 1
    assert state.opened
    assert all(connection.closed for connection in state.opened)
    assert all(connection.close_calls == 1 for connection in state.opened)
    assert {config.host for config in state.opened_configs} == {"coordinator"}


def _assert_only_acknowledged_drop_attempts(state, local_timeout, held):
    resources = [_drop_resource(statement) for statement in state.commands]
    if local_timeout:
        assert len(held) == 1
        assert resources == [(_TARGET, None)]
        assert state.host_commands[state.remote] == []
    else:
        assert held == []
        assert resources == [(_TARGET, None), (_PHYSICAL, "storage")]
        assert state.delayed_cluster_drops == [("storage", _PHYSICAL)]


@pytest.mark.parametrize("failure", ["remote-retry-disabled", "coordinator-local-timeout"])
def test_bounded_drop_propagates_timeout_without_unauthorized_host_recovery(
    monkeypatch, write_sql_connections, failure
):
    state = install_drop_scope(monkeypatch, write_sql_connections, driver="native")
    name, address, _port = state.host_endpoints[state.remote]
    state.host_endpoints[state.remote] = (name, address, 9441)
    identity, _port = state.server_identities[state.remote]
    state.server_identities[state.remote] = (identity, 9441)
    local_timeout = failure == "coordinator-local-timeout"
    if not local_timeout:
        state.delay_cluster_drops.add(("storage", _PHYSICAL))
    before = state.snapshot()
    expected = deepcopy(before)
    if not local_timeout:
        expected[state.coordinator].pop(_TARGET)
    ticks = count(step=301)
    monkeypatch.setattr(
        drop_execution,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _seconds: None),
    )
    held = []
    roles = []
    timed_out = []
    host_attempts = []

    def open_coordinator(key):
        connection = sql.get_sql_connection(key)
        if local_timeout:
            _delay_local_acknowledgement(monkeypatch, state, state.opened[-1], held)
        return connection

    manager = BoundedConnectionManager(
        "ch", 1, role="bounded drop fault", open_connection=open_coordinator
    )

    def run(role, operation):
        roles.append(role)
        try:
            return manager.run(role, lambda ref: operation(ref["connection"]))
        except TimeoutError as error:
            timed_out.append(error)
            raise

    def forbidden_host(host, _operation):
        host_attempts.append(host)
        pytest.fail("A disabled or coordinator-local timeout attempted host recovery.")

    try:
        with pytest.raises(TimeoutError, match="ch_retry_per_host_drops=True") as caught:
            drop_ch_distributed_table_pair_bounded(
                _TARGET,
                "storage",
                query_label=None,
                ch_retry_per_host_drops=local_timeout,
                connection_runner=run,
                host_connection_runner=forbidden_host,
            )
    finally:
        manager.close()

    assert timed_out == [caught.value]
    assert timed_out[0] is caught.value
    assert host_attempts == []
    assert "resolve_target_drop_hosts" not in roles
    assert "validate_target_pair_drop" not in roles
    _assert_protected_state_and_closed_pool(state, before, expected, manager)
    _assert_only_acknowledged_drop_attempts(state, local_timeout, held)


def _bounded_runners(monkeypatch, state, worker_failure=None):
    manager = BoundedConnectionManager(
        "ch", 1, role="bounded certified overlap", open_connection=sql.get_sql_connection
    )
    roles, hosts, worker_commands = [], [], []

    def run(role, operation):
        roles.append(role)
        return manager.run(role, lambda ref: operation(ref["connection"]))

    def run_host(endpoint, operation):
        # Recovery must release its coordinator lease before acquiring the same
        # pool's sole permit. Check first so a regression cannot hang this test.
        assert manager._available.qsize() == 1
        hosts.append((str(endpoint), endpoint.tcp_port))

        def open_worker():
            connection = get_ch_connection_for_host("ch", endpoint)
            raw = state.opened[-1]
            command = raw.command

            def worker_command(statement, settings=None):
                assert _drop_resource(statement) == (_PHYSICAL, None)
                worker_commands.append(statement)
                if worker_failure is not None:
                    raise worker_failure
                return command(statement, settings=settings)

            monkeypatch.setattr(raw, "command", worker_command)
            return connection

        return manager.run_with_connection("bounded recovery host", open_worker, operation)

    return manager, run, run_host, roles, hosts, worker_commands


def _assert_bounded_connections_released(manager, state):
    assert manager._available.qsize() == 1
    assert manager.run("reacquire after drop", lambda _ref: "released") == "released"
    assert manager.high_water_mark == 1
    manager.close()
    assert state.opened
    assert all(connection.closed and connection.close_calls == 1 for connection in state.opened)


@pytest.mark.parametrize("wait", [False, True], ids=["without-wait", "with-wait"])
def test_bounded_drop_partial_overlap_submits_only_pending_host_then_skips_full_overlap(
    monkeypatch, write_sql_connections, wait
):
    state = install_drop_scope(monkeypatch, write_sql_connections, driver="native")
    expected = deepcopy(state.snapshot())
    expected[state.coordinator].pop(_PHYSICAL)
    expected[state.remote].pop(_PHYSICAL)
    manager, run, host, roles, hosts, worker_commands = _bounded_runners(monkeypatch, state)

    def certify(connection):
        # These are three independently certified complete resources, matching
        # local + all-host + remote-only scopes in one selected drop batch.
        plans = [
            certify_explicit_drop(connection, _PHYSICAL, cluster)
            for cluster in (None, "core", "storage")
        ]
        assert all(plan.config == plans[0].config for plan in plans)
        assert [len(plan.resources[0].table_identities) for plan in plans] == [1, 2, 1]
        return VerifiedDropLayout(
            tuple(resource for plan in plans for resource in plan.resources), plans[0].config
        )

    try:
        execute_bounded_verified_drop(
            get_backend_adapter("ch"),
            run,
            certify,
            DropExecutionOptions(
                wait_for_absence=wait,
                host_connection_runner=host,
            ),
        )
        assert state.snapshot() == expected
        expected_roles = [
            "certify_target_drop",
            "preflight_target_drop",
            "resolve_target_drop_positions",
            "drop_target_pair",
            "resolve_target_drop_positions",
        ]
        if wait:
            expected_roles.append("validate_target_pair_drop")
        expected_roles.append("resolve_target_drop_positions")
        if wait:
            expected_roles.append("validate_target_pair_drop")
        assert roles == expected_roles
        assert hosts == [(state.host_endpoints[state.remote][1], 9000)]
        assert len(worker_commands) == 1
        assert [_drop_resource(statement) for statement in state.commands] == [
            (_PHYSICAL, None),
            (_PHYSICAL, None),
        ]
        assert len(state.host_commands[state.coordinator]) == 1
        assert len(state.host_commands[state.remote]) == 1
        assert state.delayed_cluster_drops == []
        _assert_bounded_connections_released(manager, state)
    finally:
        manager.close()


@pytest.mark.parametrize("worker_fails", [False, True], ids=["recovered", "worker-failure"])
def test_bounded_drop_acknowledged_wait_recovery_releases_cap_one_and_preserves_primary(
    monkeypatch, write_sql_connections, worker_fails
):
    state = install_drop_scope(monkeypatch, write_sql_connections, driver="native")
    state.delay_cluster_drops.add(("storage", _PHYSICAL))
    before = state.snapshot()
    expected = deepcopy(before)
    expected[state.coordinator].pop(_TARGET)
    if not worker_fails:
        expected[state.remote].pop(_PHYSICAL)
    failure = PermissionError("selected recovery host rejected the local DROP")
    manager, run, host, roles, hosts, worker_commands = _bounded_runners(
        monkeypatch, state, failure if worker_fails else None
    )

    def execute():
        execute_bounded_verified_drop(
            get_backend_adapter("ch"),
            run,
            lambda connection: certify_existing_drop(
                connection, _TARGET, DropIntent("ch", "storage")
            ),
            DropExecutionOptions(
                wait_for_absence=True,
                timeout_seconds=0,
                poll_interval_seconds=0,
                host_connection_runner=host,
            ),
        )

    try:
        if worker_fails:
            with pytest.raises(AmbiguousSqlReplaceError) as error:
                execute()
            assert type(error.value) is AmbiguousSqlReplaceError
            assert error.value.__cause__ is failure
        else:
            execute()
        assert state.snapshot() == expected
        assert (
            state.snapshot()[state.coordinator][_PHYSICAL] == before[state.coordinator][_PHYSICAL]
        )
        expected_roles = [
            "certify_target_drop",
            "preflight_target_drop",
            "resolve_target_drop_positions",
            "drop_target_pair",
            "resolve_target_drop_positions",
            "drop_target_pair",
            "resolve_target_drop_hosts",
        ]
        assert roles == expected_roles + ([] if worker_fails else ["validate_target_pair_drop"])
        assert hosts == [(state.host_endpoints[state.remote][1], 9000)]
        assert len(worker_commands) == 1
        assert len(state.commands) == (2 if worker_fails else 3)
        assert state.delayed_cluster_drops == [("storage", _PHYSICAL)]
        assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
        _assert_bounded_connections_released(manager, state)
    finally:
        manager.close()
