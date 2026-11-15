"""A delayed certified drop cannot retain capacity beyond its wait boundary."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch import drop_execution
from analytics_toolkit.sql.backends.ch.drop_bounded import execute_bounded_verified_drop
from analytics_toolkit.sql.backends.ch.drop_execution import (
    DropExecutionOptions,
    execute_verified_drop,
)
from analytics_toolkit.sql.backends.ch.drop_layout import DropIntent, certify_existing_drop
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError
from analytics_toolkit.sql.dml.transfer.runtime.connection_pool import BoundedConnectionManager
from analytics_toolkit.sql.execution import cancellation
from analytics_toolkit.sql.execution.cancellation import (
    AsyncSqlCancelled,
    SqlCancellationScope,
    activate_cancellation_scope,
)

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import install_drop_scope
from tests.sql._support.reconfigure_conversion_scope import _scope_sql

_TARGET = "analytics.events"
_PHYSICAL = "analytics.events_shard"


class _Clock:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds


class _ScheduledCancellationEvent:
    """A raw event whose wait advances virtual time to the cancellation signal."""

    def __init__(self, clock, cancel_at):
        self.clock = clock
        self.cancel_at = cancel_at
        self.signalled = False
        self.waits = []

    def is_set(self):
        return self.signalled or self.clock.elapsed >= self.cancel_at

    def set(self):
        self.signalled = True

    def wait(self, timeout):
        self.waits.append(timeout)
        remaining = max(0.0, self.cancel_at - self.clock.elapsed)
        self.clock.elapsed += min(timeout, remaining)
        return self.is_set()


def _install_wait(monkeypatch, write_sql_connections):
    state = install_drop_scope(monkeypatch, write_sql_connections, driver="native")
    state.delay_cluster_drops.add(("storage", _PHYSICAL))
    expected = deepcopy(state.snapshot())
    expected[state.coordinator].pop(_TARGET)
    clock = _Clock()
    raw_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    monkeypatch.setattr(drop_execution, "time", raw_time)
    monkeypatch.setattr(cancellation, "time", raw_time)
    manager = BoundedConnectionManager(
        "ch", 1, role="certified drop wait", open_connection=sql.get_sql_connection
    )
    return state, expected, clock, manager


def _execute(manager, options):
    def drop(ref):
        connection = ref["connection"]
        certificate = certify_existing_drop(connection, _TARGET, DropIntent("ch", "storage"))
        assert {resource.role for resource in certificate.resources} == {"facade", "physical"}
        execute_verified_drop(get_backend_adapter("ch"), connection, certificate, options)

    manager.run("drop while holding one coordinator lease", drop)


def _assert_released(manager, state, expected):
    # Reacquiring the sole permit exercises the real finally-path accounting.
    assert manager._available.qsize() == 1
    assert manager.run("reuse released coordinator capacity", lambda _ref: "available") == (
        "available"
    )
    assert manager.high_water_mark == 1
    assert state.snapshot() == expected
    assert len(state.commands) == 2
    assert state.delayed_cluster_drops == [("storage", _PHYSICAL)]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]


def test_drop_wait_boundaries_poll_interval_cannot_exceed_timeout_or_hold_lease(
    monkeypatch, write_sql_connections
):
    state, expected, clock, manager = _install_wait(monkeypatch, write_sql_connections)
    try:
        with pytest.raises(TimeoutError, match="after 1 second"):
            _execute(
                manager,
                DropExecutionOptions(
                    wait_for_absence=True,
                    timeout_seconds=1,
                    poll_interval_seconds=60,
                    retry_per_host_drops=False,
                ),
            )
        _assert_released(manager, state, expected)
    finally:
        manager.close()

    assert clock.elapsed <= 1.0
    assert clock.sleeps == [1.0]
    assert state.opened
    assert all(connection.closed and connection.close_calls == 1 for connection in state.opened)


def test_drop_wait_boundaries_cancellation_interrupts_poll_and_releases_lease(
    monkeypatch, write_sql_connections
):
    state, expected, clock, manager = _install_wait(monkeypatch, write_sql_connections)
    scope = SqlCancellationScope()
    signal = _ScheduledCancellationEvent(clock, cancel_at=0.25)
    monkeypatch.setattr(scope, "_cancelled", signal)
    try:
        with activate_cancellation_scope(scope), pytest.raises(AsyncSqlCancelled):
            _execute(
                manager,
                DropExecutionOptions(
                    wait_for_absence=True,
                    timeout_seconds=300,
                    poll_interval_seconds=60,
                    retry_per_host_drops=False,
                ),
            )
        assert scope.cancelled
        _assert_released(manager, state, expected)
    finally:
        manager.close()

    assert clock.elapsed <= 0.25
    assert clock.sleeps == []
    assert signal.waits
    assert all(0 < delay <= 0.1 for delay in signal.waits)
    assert state.opened
    assert all(connection.closed and connection.close_calls == 1 for connection in state.opened)


def test_drop_wait_boundaries_raw_submission_timeout_is_not_absence_recovery(
    monkeypatch, write_sql_connections
):
    state = install_drop_scope(monkeypatch, write_sql_connections, driver="native")
    expected = deepcopy(state.snapshot())
    expected[state.coordinator].pop(_TARGET)
    expected[state.remote].pop(_PHYSICAL)
    failure = TimeoutError("physical DROP acknowledgment lost after submission")
    submitted = []
    roles = []
    host_recovery = []

    def open_coordinator(key):
        connection = sql.get_sql_connection(key)
        raw = state.opened[-1]
        command = raw.command

        def lose_physical_acknowledgment(statement, settings=None):
            result = command(statement, settings=settings)
            cluster, _body = _scope_sql(strip_query_label(statement))
            if cluster == "storage":
                assert submitted == []
                submitted.append((statement, state.snapshot()))
                raise failure
            return result

        monkeypatch.setattr(raw, "command", lose_physical_acknowledgment)
        return connection

    manager = BoundedConnectionManager(
        "ch", 1, role="uncertain drop submission", open_connection=open_coordinator
    )

    def run(role, operation):
        roles.append(role)
        return manager.run(role, lambda ref: operation(ref["connection"]))

    def forbidden_host(host, _operation):
        host_recovery.append(host)
        pytest.fail("An unacknowledged DROP submission must not enter absence recovery.")

    try:
        with pytest.raises(AmbiguousSqlReplaceError) as error:
            execute_bounded_verified_drop(
                get_backend_adapter("ch"),
                run,
                lambda connection: certify_existing_drop(
                    connection, _TARGET, DropIntent("ch", "storage")
                ),
                DropExecutionOptions(
                    wait_for_absence=True,
                    timeout_seconds=1,
                    poll_interval_seconds=60,
                    retry_per_host_drops=True,
                    host_connection_runner=forbidden_host,
                ),
            )
        assert type(error.value) is AmbiguousSqlReplaceError
        assert error.value.__cause__ is failure
        assert manager._available.qsize() == 1
        assert manager.run("reuse capacity after raw timeout", lambda _ref: True)
        assert manager.high_water_mark == 1
    finally:
        manager.close()

    assert len(submitted) == 1
    assert submitted[0][1] == state.snapshot() == expected
    assert roles == [
        "certify_target_drop",
        "preflight_target_drop",
        "resolve_target_drop_positions",
        "drop_target_pair",
        "resolve_target_drop_positions",
        "drop_target_pair",
    ]
    assert host_recovery == []
    assert len(state.commands) == 2
    assert state.delayed_cluster_drops == []
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.opened
    assert all(connection.closed and connection.close_calls == 1 for connection in state.opened)
