"""Bounded drop failures never borrow host capacity outside their selected policy."""

from __future__ import annotations

from copy import deepcopy
from itertools import count
from types import SimpleNamespace

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch import drop_execution
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.lifecycle import drop_ch_distributed_table_pair_bounded
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
