"""Raw endpoint faults cannot expand or replay certified ClickHouse drops."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.drop_execution import (
    DropExecutionOptions,
    execute_drop_recovery,
    execute_verified_drop,
    prepare_drop_recovery,
)
from analytics_toolkit.sql.backends.ch.drop_layout import (
    DropIntent,
    certify_existing_drop,
    certify_explicit_drop,
)
from analytics_toolkit.sql.backends.ch.lifecycle import drop_ch_table
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.connection.get_sql_connection import get_ch_connection_for_host
from analytics_toolkit.sql.execution.backend_options import current_backend_operation

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import _DropConnection, install_drop_scope
from tests.sql._support.reconfigure_physical_faults import reselect_private_config

_TARGET = "analytics.events"
_PHYSICAL = "analytics.events_shard"
_REPLACEMENT_SERVER = "00000000-0000-0000-0000-000000000099"


@pytest.fixture
def fault_case(monkeypatch, write_sql_connections):
    states = []

    def install(*, driver="native"):
        state = install_drop_scope(monkeypatch, write_sql_connections, driver=driver)
        states.append(state)
        return state

    yield install

    for state in states:
        assert state.opened
        assert all(connection.closed for connection in state.opened)
        assert all(connection.close_calls == 1 for connection in state.opened)


@contextmanager
def _connection():
    connection = sql.get_sql_connection("ch")
    try:
        yield connection
    finally:
        connection.close()


def _delayed_options(**changes):
    return DropExecutionOptions(
        wait_for_absence=True,
        timeout_seconds=0,
        poll_interval_seconds=0,
        **changes,
    )


def _execute_pair(connection, options):
    plan = certify_existing_drop(connection, _TARGET, DropIntent("ch", "storage"))
    execute_verified_drop(get_backend_adapter("ch"), connection, plan, options)


def _foreign_remote_table(state):
    state._create(
        state.remote,
        parse_create_table(
            f"CREATE TABLE {_TARGET} (id UInt64) ENGINE=MergeTree ORDER BY id", _TARGET
        ),
        state.foreign_rows,
    )


def _only_facade_removed(state, before):
    expected = deepcopy(before)
    expected[state.coordinator].pop(_TARGET)
    return expected


def test_configuration_guard_rejects_changed_alias_before_a_new_client_or_drop(
    fault_case, write_sql_connections
):
    state = fault_case()
    before = state.snapshot()
    observed = []

    def open_with_changed_configuration(_connection, _statement):
        operation = current_backend_operation()
        guards = (
            operation.resources.get("connection_configuration_guards", {})
            if operation is not None
            else {}
        )
        if not any(isinstance(key, tuple) and key[0] == "verified_drop" for key in guards):
            return
        state.before_query = None
        observed.append((operation, len(state.opened_configs)))
        reselect_private_config(write_sql_connections)
        sql.get_sql_connection("ch")

    state.before_query = open_with_changed_configuration

    with pytest.raises(
        InvalidSqlInputError, match="configuration changed during the verified drop"
    ) as error:
        sql.drop_tables("ch", _TARGET, ch_cluster="storage")

    assert len(observed) == 1
    operation, opened_before_rejection = observed[0]
    assert len(state.opened_configs) == opened_before_rejection
    assert operation.resources.get("connection_configuration_guards", {}) == {}
    assert "private-config-change-must-stay-hidden" not in str(error.value)
    assert state.snapshot() == before
    assert state.commands == []


def test_http_rejects_two_native_instances_at_one_address_before_any_drop(fault_case):
    state = fault_case(driver="http")
    second = "native_second"
    state.host_tables[second] = {}
    state.host_commands[second] = []
    address = state.host_endpoints[state.remote][1]
    state.host_endpoints[second] = ("native-second", address, 9001)
    state.server_identities[second] = (_REPLACEMENT_SERVER, 9001)
    physical = state.host_tables[state.remote][_PHYSICAL]
    state._create(second, physical.create.copy(), deepcopy(physical.rows))
    state.clusters["storage"] = (state.remote, second)
    before = state.snapshot()

    with pytest.raises(
        InvalidSqlInputError,
        match="HTTP drop recovery cannot distinguish multiple native instances",
    ):
        sql.drop_tables("ch", _TARGET, ch_cluster="storage")

    assert state.snapshot() == before
    assert state.commands == []
    assert all(commands == [] for commands in state.host_commands.values())
    assert all(config.host != address for config in state.opened_configs)


@pytest.mark.parametrize(
    "rows",
    [[], [(2,)], [(None,)], [(0, 1)], [(0,), (1,)]],
    ids=["missing", "outside-boolean-domain", "null", "extra-field", "extra-row"],
)
def test_local_absence_requires_one_boolean_result_after_the_acknowledged_drop(fault_case, rows):
    state = fault_case()
    before = state.snapshot()
    corrupted = []

    def malformed_exists(_connection, statement, result):
        if strip_query_label(statement) == f"EXISTS TABLE {_TARGET}":
            assert result.result_rows == [(0,)]
            result.result_rows = rows
            corrupted.append(statement)

    state.after_query = malformed_exists

    with _connection() as connection, pytest.raises(
        InvalidSqlInputError, match="local ClickHouse drop absence result"
    ):
        _execute_pair(connection, _delayed_options())

    assert len(corrupted) == 1
    assert state.snapshot() == _only_facade_removed(state, before)
    assert len(state.commands) == 1
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("endpoint", "selected ClickHouse drop recovery endpoint identity"),
        ("table", "table changed after drop ownership was verified"),
    ],
)
def test_recovery_rejects_changed_endpoint_or_table_identity_after_cluster_ack(
    fault_case, fault, message
):
    state = fault_case()
    _foreign_remote_table(state)
    state.delay_cluster_drops.add(("storage", _PHYSICAL))
    before = state.snapshot()
    injected = []

    def replace_worker_identity(connection, statement):
        if (
            injected
            or len(state.commands) != 2
            or connection.host != state.remote
            or strip_query_label(statement) != "SELECT serverUUID(), tcpPort()"
        ):
            return
        assert len(state.commands) == 2
        if fault == "endpoint":
            _old_uuid, port = state.server_identities[state.remote]
            state.server_identities[state.remote] = (_REPLACEMENT_SERVER, port)
        else:
            old = state.host_tables[state.remote].pop(_PHYSICAL)
            state._create(state.remote, old.create.copy(), deepcopy(old.rows))
        injected.append(state.snapshot())

    state.before_query = replace_worker_identity

    with _connection() as connection, pytest.raises(InvalidSqlInputError, match=message):
        _execute_pair(connection, _delayed_options())

    assert len(injected) == 1
    assert state.snapshot() == injected[0]
    expected = _only_facade_removed(state, before)
    if fault == "table":
        replacement = state.snapshot()[state.remote][_PHYSICAL]
        assert replacement["uuid"] != before[state.remote][_PHYSICAL]["uuid"]
        expected[state.remote][_PHYSICAL] = replacement
    assert state.snapshot() == expected
    assert len(state.commands) == 2
    assert state.delayed_cluster_drops == [("storage", _PHYSICAL)]
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.remote, _TARGET) == state.foreign_rows


def test_already_applied_queued_drop_needs_no_second_worker_submission(fault_case):
    state = fault_case()
    state.delay_cluster_drops.add(("storage", _PHYSICAL))
    before = state.snapshot()
    completed = []

    def complete_queued_drop(connection, statement):
        if (
            not completed
            and len(state.commands) == 2
            and connection.host == state.remote
            and strip_query_label(statement) == "SELECT serverUUID(), tcpPort()"
        ):
            assert len(state.commands) == 2
            completed.append(state.host_tables[state.remote].pop(_PHYSICAL))

    state.before_query = complete_queued_drop

    with _connection() as connection:
        _execute_pair(connection, _delayed_options())

    expected = _only_facade_removed(state, before)
    expected[state.remote].pop(_PHYSICAL)
    assert state.snapshot() == expected
    assert len(completed) == 1
    assert len(state.commands) == 2
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows


def test_scoped_coordinator_recovery_reuses_the_existing_connection_once(fault_case):
    state = fault_case()
    state.delay_cluster_drops.add(("coordinator_only", _TARGET))
    before = state.snapshot()
    opened_after_ack = []

    def record_open_count(_connection, _statement):
        if len(state.commands) == 1 and not opened_after_ack:
            opened_after_ack.append(len(state.opened_configs))

    state.before_query = record_open_count

    with _connection() as connection:
        drop_ch_table(
            connection,
            _TARGET,
            ch_cluster="coordinator_only",
            wait_for_absence=True,
            wait_timeout_seconds=0,
            wait_poll_interval_seconds=0,
        )

    assert state.snapshot() == _only_facade_removed(state, before)
    assert opened_after_ack == [len(state.opened_configs)]
    assert state.delayed_cluster_drops == [("coordinator_only", _TARGET)]
    assert len(state.commands) == 2
    assert state.commands[0].endswith("ON CLUSTER coordinator_only")
    assert "ON CLUSTER" not in state.commands[1]
    assert state.host_commands[state.remote] == []


def test_real_local_certificate_produces_no_remote_recovery_targets(fault_case):
    state = fault_case()
    before = state.snapshot()
    attempted = []

    def unwanted_factory(host):
        attempted.append(host)
        pytest.fail("A local-only certificate must not open a remote recovery client.")

    with _connection() as connection:
        plan = certify_explicit_drop(connection, _TARGET, None)
        assert len(plan.resources) == 1
        assert plan.resources[0].scope is None
        targets = prepare_drop_recovery(connection, plan.resources[0])
        assert targets == ()
        execute_drop_recovery(
            get_backend_adapter("ch"),
            connection,
            plan,
            targets,
            DropExecutionOptions(per_host_connection_factory=unwanted_factory),
        )

    assert attempted == []
    assert state.snapshot() == before
    assert state.commands == []


@pytest.mark.parametrize("failure_kind", ["factory", "write-and-close"])
def test_worker_failure_preserves_primary_error_and_remaining_data(fault_case, failure_kind):
    state = fault_case()
    _foreign_remote_table(state)
    state.delay_cluster_drops.add(("storage", _PHYSICAL))
    before = state.snapshot()
    failure = RuntimeError(
        "endpoint unavailable" if failure_kind == "factory" else "worker write failed"
    )
    attempts = []
    workers = []

    class FailedWorker(_DropConnection):
        def command(self, statement, settings=None):
            attempts.append(statement)
            raise failure

        def close(self):
            super().close()
            message = "secondary worker close failed"
            raise RuntimeError(message)

    def factory(endpoint):
        assert (str(endpoint), endpoint.tcp_port) == (
            state.host_endpoints[state.remote][1],
            state.host_endpoints[state.remote][2],
        )
        if failure_kind == "factory":
            raise failure
        worker = FailedWorker(state, state.remote, native=True)
        state.opened.append(worker)
        workers.append(worker)
        return worker

    with _connection() as connection, pytest.raises(RuntimeError) as error:
        _execute_pair(connection, _delayed_options(per_host_connection_factory=factory))

    assert error.value is failure
    assert state.snapshot() == _only_facade_removed(state, before)
    assert len(state.commands) == 2
    assert len(attempts) == (1 if failure_kind == "write-and-close" else 0)
    assert all(
        "events_shard" in statement and "ON CLUSTER" not in statement for statement in attempts
    )
    assert all(worker.close_calls == 1 and worker.closed for worker in workers)
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.remote, _TARGET) == state.foreign_rows


def test_disabled_recovery_preserves_wait_timeout_without_opening_a_worker(fault_case):
    state = fault_case()
    state.delay_cluster_drops.add(("storage", _PHYSICAL))
    before = state.snapshot()
    attempted = []

    def factory(host):
        attempted.append(host)
        pytest.fail("Disabled recovery must not open a worker.")

    with _connection() as connection, pytest.raises(TimeoutError, match="remains visible"):
        _execute_pair(
            connection,
            _delayed_options(retry_per_host_drops=False, per_host_connection_factory=factory),
        )

    assert attempted == []
    assert state.snapshot() == _only_facade_removed(state, before)
    assert len(state.commands) == 2
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.remote, _PHYSICAL) == [(11,), (12,), (13,)]


def test_provided_host_runner_uses_real_endpoint_and_cleans_only_its_resource(fault_case):
    state = fault_case()
    _foreign_remote_table(state)
    state.delay_cluster_drops.add(("storage", _PHYSICAL))
    before = state.snapshot()
    hosts = []

    def runner(host, operation):
        hosts.append((str(host), host.tcp_port))
        worker = get_ch_connection_for_host("ch", host)
        try:
            operation(worker)
        finally:
            worker.close()

    with _connection() as connection:
        _execute_pair(connection, _delayed_options(host_connection_runner=runner))

    expected = _only_facade_removed(state, before)
    expected[state.remote].pop(_PHYSICAL)
    assert state.snapshot() == expected
    assert hosts == [(state.host_endpoints[state.remote][1], 9000)]
    assert len(state.commands) == 3
    assert state.rows(state.coordinator, _PHYSICAL) == state.foreign_rows
    assert state.rows(state.remote, _TARGET) == state.foreign_rows
