from __future__ import annotations

import types
from concurrent.futures import CancelledError
from dataclasses import replace
from functools import partial

import clickhouse_connect
import clickhouse_driver
import psycopg2
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend
from analytics_toolkit.sql.backends.ch import config as ch_config
from analytics_toolkit.sql.backends.ch import native_client
from analytics_toolkit.sql.backends.gp import connectivity
from analytics_toolkit.sql.backends.trino import config as trino_config
from analytics_toolkit.sql.connection import validation as validation_module
from analytics_toolkit.sql.connection.validation import (
    validating,
    validation_scope,
    validation_timeout,
)

from tests.sql._support.connection_config import (
    _direct_trino_config,
    _expected_trino_read_options,
    _install_fake_trino,
    config_module,
    connection_module,
)

_PRIVATE_ERROR = "private-password-in-driver-error"
_NETWORK_FORBIDDEN = "Network access is forbidden"
_CLOSE_ERROR = "close"


@pytest.fixture
def probe_clock(monkeypatch):
    clock = types.SimpleNamespace(now=100.0)
    monkeypatch.setattr(validation_module.time, "monotonic", lambda: clock.now)
    return clock


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize("failure", [None, "execute", "fetch"])
def test_connectivity_requires_consumed_probe_and_closes_once(
    backend, failure, monkeypatch, write_sql_connections
):
    events = []
    connection = types.SimpleNamespace()

    class Cursor:
        def execute(self, statement):
            events.append(statement)
            if failure == "execute":
                raise ConnectionError(_PRIVATE_ERROR)

        def fetchall(self):
            events.append("fetch")
            if failure == "fetch":
                raise ConnectionError(_PRIVATE_ERROR)
            return [(1,)]

        def close(self):
            events.append("cursor-close")

    class Result:
        @property
        def result_rows(self):
            return Cursor().fetchall()

    def query(statement):
        Cursor().execute(statement)
        return Result()

    connection.cursor = Cursor
    connection.query = query
    connection.close = lambda: events.append("connection-close")
    write_sql_connections(
        {
            "target": {
                "type": backend,
                "host": "host",
                "user": "user",
                "password": "",
                "database": "db",
            }
        }
    )
    monkeypatch.setattr(config_module, "_open_validation_connection", lambda _key: connection)
    result = sql.validate_connections(["target"], connect=True)[0]
    assert result.connected == (failure is None)
    assert events.count("SELECT 1") == 1
    assert events.count("connection-close") == 1
    assert events.count("fetch") == (failure != "execute")
    if failure:
        assert "private-password" not in result.error
        assert "ConnectionError" in result.error


def test_network_free_validation_never_opens(monkeypatch):
    def unexpected(_key):
        raise AssertionError(_NETWORK_FORBIDDEN)

    monkeypatch.setattr(config_module, "_open_validation_connection", unexpected)
    assert sql.validate_connections(["gp"], connect=False)[0].connected is None


def test_close_failure_does_not_invalidate_completed_probe(monkeypatch):
    class Cursor:
        def execute(self, _sql):
            pass

        def fetchall(self):
            return [(1,)]

        def close(self):
            raise OSError(_CLOSE_ERROR)

    class Connection:
        cursor = Cursor

        def close(self):
            raise OSError(_CLOSE_ERROR)

    monkeypatch.setattr(config_module, "_open_validation_connection", lambda _key: Connection())
    assert sql.validate_connections(["gp"], connect=True)[0].connected is True


@pytest.mark.parametrize(("configured", "expected"), [(None, 5), (17, 5), (2, 2)])
def test_trino_validation_budget_and_no_retry(configured, expected, monkeypatch, probe_clock):
    del probe_clock
    calls = []
    _install_fake_trino(monkeypatch, calls)
    with validation_scope():
        trino_config.open_connection(
            _direct_trino_config(request_timeout=configured),
            parse_verify_value=lambda value: value,
            resolve_ca_certs=lambda *_args: None,
        )
    assert calls[0]["request_timeout"] == expected
    assert calls[0]["max_attempts"] == 1
    assert len(calls) == 1
    for name, value in _expected_trino_read_options().items():
        assert calls[0][name] == value


@pytest.mark.parametrize("driver", ["http", "native"])
def test_clickhouse_validation_has_bounded_driver_timeouts(driver, monkeypatch, probe_clock):
    del probe_clock
    captured = []

    class Client:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    if driver == "http":
        monkeypatch.setattr(clickhouse_connect, "get_client", Client)
    else:
        monkeypatch.setattr(clickhouse_driver, "Client", Client)
        monkeypatch.setattr(native_client, "NativeClickHouseClient", lambda client: client)
    config = config_module.get_connection_config("ch")

    config = replace(config, driver=driver, connect_timeout=2, send_receive_timeout=25)
    with validation_scope():
        ch_config.open_connection(
            config, parse_verify_value=lambda value: value, resolve_ch_ca_certs=lambda _config: None
        )
    assert captured[0]["connect_timeout"] == 2
    assert captured[0]["send_receive_timeout"] == 5
    if driver == "http":
        assert captured[0]["query_retries"] == 0
    assert captured[0].get("settings") == config.settings


def test_greenplum_validation_uses_bounded_async_polling(monkeypatch, probe_clock):
    del probe_clock

    events = []
    calls = []
    cursor = types.SimpleNamespace(
        execute=events.append,
        fetchall=lambda: events.append("fetch") or [(1,)],
        close=lambda: events.append("cursor-close"),
    )
    driver = types.SimpleNamespace(
        async_=True,
        cursor=lambda: cursor,
        poll=lambda: 0,
        close=lambda: events.append("connection-close"),
    )
    monkeypatch.setattr(psycopg2, "connect", lambda **kwargs: calls.append(kwargs) or driver)
    result = sql.validate_connections(["gp"], connect=True)[0]
    assert result.connected
    assert calls[0]["async_"] is True
    assert calls[0]["connect_timeout"] == 5
    assert events == ["SELECT 1", "fetch", "cursor-close", "connection-close"]


def test_greenplum_validation_timeout_is_finite_and_closes(monkeypatch):

    events = []
    driver = types.SimpleNamespace(
        async_=True,
        poll=lambda: 2,
        fileno=lambda: 123,
        close=lambda: events.append("close"),
    )
    monkeypatch.setattr(psycopg2, "connect", lambda **_kwargs: driver)
    budgets = []
    monkeypatch.setattr(
        connectivity.select,
        "select",
        lambda _read, _write, _error, timeout: budgets.append(timeout) or ([], [], []),
    )
    result = sql.validate_connections(["gp"], connect=True)[0]
    assert not result.connected
    assert "TimeoutError" in result.error
    assert events == ["close"]
    assert 0 < budgets[0] <= 5


def test_greenplum_validation_shares_budget_across_open_and_probe(monkeypatch, probe_clock):
    events = []
    states = iter([2, 0, 2, 0])
    budgets = []

    def execute(statement):
        events.append(statement)
        probe_clock.now += 0.75

    cursor = types.SimpleNamespace(
        execute=execute,
        fetchall=lambda: events.append("fetch") or [(1,)],
        close=lambda: events.append("cursor-close"),
    )
    driver = types.SimpleNamespace(
        async_=True,
        cursor=lambda: cursor,
        poll=lambda: next(states),
        fileno=lambda: 123,
        close=lambda: events.append("connection-close"),
    )

    def connect(**_kwargs):
        probe_clock.now += 1.25
        return driver

    def select_ready(read, write, _error, timeout):
        budgets.append(timeout)
        probe_clock.now += 0.5
        return read, write, []

    monkeypatch.setattr(psycopg2, "connect", connect)
    monkeypatch.setattr(connectivity.select, "select", select_ready)
    result = sql.validate_connections(["gp"], connect=True)[0]

    assert result.connected is True
    assert budgets == [3.75, 2.5]
    assert events == ["SELECT 1", "fetch", "cursor-close", "connection-close"]


@pytest.mark.parametrize("phase", ["open", "execute", "fetch"])
def test_greenplum_validation_rejects_expired_ready_result(phase, monkeypatch, probe_clock):
    events = []

    def advance_if_expired(current):
        if phase == current:
            probe_clock.now += 5

    def execute(statement):
        events.append(statement)
        advance_if_expired("execute")

    def fetchall():
        events.append("fetch")
        advance_if_expired("fetch")
        return [(1,)]

    cursor = types.SimpleNamespace(
        execute=execute, fetchall=fetchall, close=lambda: events.append("cursor-close")
    )
    driver = types.SimpleNamespace(
        async_=True,
        cursor=lambda: cursor,
        poll=lambda: 0,
        close=lambda: events.append("connection-close"),
    )

    def connect(**_kwargs):
        advance_if_expired("open")
        return driver

    monkeypatch.setattr(psycopg2, "connect", connect)
    result = sql.validate_connections(["gp"], connect=True)[0]

    assert result.connected is False
    assert "TimeoutError" in result.error
    assert events.count("SELECT 1") == (phase != "open")
    assert events.count("cursor-close") == (phase != "open")
    assert events.count("connection-close") == 1


@pytest.mark.parametrize("state", [1, 2])
def test_greenplum_poll_waits_for_ready_descriptor_then_finishes(state, monkeypatch):
    states = iter([state, 0])
    driver = types.SimpleNamespace(poll=lambda: next(states), fileno=lambda: 123)
    calls = []

    def select_ready(read, write, error, timeout):
        calls.append((read, write, error, timeout))
        return read, write, []

    monkeypatch.setattr(connectivity.select, "select", select_ready)
    connectivity.wait_ready(driver, 2)
    expected = ([123], [], []) if state == 1 else ([], [123], [])
    assert calls[0][:3] == expected
    assert 0 < calls[0][3] <= 2


def test_greenplum_poll_unsupported_state_and_expired_deadline(monkeypatch):
    driver = types.SimpleNamespace(poll=lambda: 3, fileno=lambda: 123)
    with pytest.raises(RuntimeError, match="Unexpected libpq"):
        connectivity.wait_ready(driver, 2)
    ticks = iter([0, 3])
    monkeypatch.setattr(connectivity.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError, match="validation budget"):
        connectivity.wait_ready(driver, 2)


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize("rows", [[], [(0,)]])
def test_validation_rejects_unexpected_probe_results(
    backend, rows, monkeypatch, write_sql_connections
):
    cursor = types.SimpleNamespace(
        execute=lambda _sql: None, fetchall=lambda: rows, close=lambda: None
    )
    connection = types.SimpleNamespace(
        cursor=lambda: cursor,
        query=lambda _sql: types.SimpleNamespace(result_rows=rows),
        close=lambda: None,
    )
    write_sql_connections(
        {
            "target": {
                "type": backend,
                "host": "host",
                "user": "user",
                "password": "",
                "database": "db",
            }
        }
    )
    monkeypatch.setattr(config_module, "_open_validation_connection", lambda _key: connection)
    result = sql.validate_connections(["target"], connect=True)[0]
    assert not result.valid
    assert result.error == "Connectivity probe failed (RuntimeError)."


def test_validation_scope_resets_after_error_and_retains_tighter_timeout(probe_clock):
    del probe_clock

    assert not validating()
    assert validation_timeout(None) is None
    message = "probe"

    def fail_in_scope():
        with validation_scope():
            assert validating()
            assert validation_timeout(2) == 2
            assert validation_timeout(0) == 5
            raise ValueError(message)

    with pytest.raises(ValueError, match=message):
        fail_in_scope()
    assert not validating()
    assert validation_timeout(8) == 8


@pytest.mark.parametrize("configured", [None, 0, -1, 25])
def test_validation_scope_shares_elapsed_budget_without_restarting(configured, probe_clock):
    with validation_scope():
        probe_clock.now += 1.25
        assert validation_timeout(configured) == 3.75
        with validation_scope():
            probe_clock.now += 1
            assert validation_timeout(configured) == 2.75
            assert validation_timeout(2) == 2
        probe_clock.now += 2.75
        with pytest.raises(TimeoutError, match="validation budget"):
            validation_timeout(configured)
    assert not validating()
    assert validation_timeout(configured) == configured


def test_validation_sanitizes_unexpected_configuration_errors(monkeypatch):
    def invalid_config(_key):
        raise RuntimeError(_PRIVATE_ERROR)

    monkeypatch.setattr(config_module, "get_connection_config", invalid_config)
    result = sql.validate_connections(["gp"])[0]
    assert result.error == "Configuration validation failed (RuntimeError)."
    assert result.connected is None


@pytest.mark.parametrize("host_factory", [False, True])
def test_connection_cancelled_after_open_preserves_cancellation_when_close_fails(
    host_factory, monkeypatch
):

    cancelled = CancelledError("cancelled")
    checks = iter([False, True])
    closed = []

    def check_cancelled():
        if next(checks):
            raise cancelled

    def close():
        closed.append(True)
        raise OSError(_CLOSE_ERROR)

    monkeypatch.setattr(connection_module, "raise_if_cancelled", check_cancelled)
    backend = "ch" if host_factory else "gp"
    adapter = get_backend(backend)
    monkeypatch.setattr(
        type(adapter),
        "open_connection",
        lambda *_args, **_kwargs: types.SimpleNamespace(close=close),
    )
    call = (
        partial(connection_module.get_ch_connection_for_host, "ch", "node")
        if host_factory
        else partial(sql.get_sql_connection, "gp")
    )
    with pytest.raises(CancelledError) as caught:
        call()
    assert caught.value is cancelled
    assert closed == [True]
