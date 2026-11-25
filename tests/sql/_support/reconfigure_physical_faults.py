"""Endpoint failures behind public physical reconfiguration and real guards."""

from __future__ import annotations

import json
from importlib import import_module

from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.connection.config import (
    get_connection_config,
    get_connections_file_path,
)
from analytics_toolkit.sql.connection.configuration_guards import check_connection_configuration
from analytics_toolkit.sql.execution.observation import observed_connection
from sqlglot import exp, parse_one

from tests.sql._support.reconfigure_independent_replicas import (
    IndependentReplicaClient,
    PhysicalHostSession,
    configure_independent_replicas,
)
from tests.sql._support.reconfigure_pair_scope import connect_pair_scope


class FaultHostSession(PhysicalHostSession):
    def __init__(self, raw, host):
        super().__init__(raw, host)
        self.close_attempts = 0

    def query(self, statement, **kwargs):
        result = super().query(statement, **kwargs)
        select = parse_one(statement, read=ClickHouseDialect)
        if select.args.get("from_") is None and self.raw.worker_identity is not None:
            result.result_rows = self.raw.worker_identity
        if select.find(exp.Count) is not None and self.raw.worker_count is not None:
            result.result_rows = self.raw.worker_count
        return result

    def command(self, statement, **kwargs):
        super().command(statement, **kwargs)
        if self.raw.extra_replacement_row:
            insert = parse_one(statement, read=ClickHouseDialect)
            self.raw.insert_rows(self.host, insert.this, [(900001,)])

    def close(self):
        self.close_attempts += 1
        super().close()
        if self.raw.fail_worker_close:
            message = "worker close failed"
            raise RuntimeError(message)


class PhysicalFaultClient(IndependentReplicaClient):
    def __init__(self, *, replicas=2):
        super().__init__(internal_replication=True, replicas=replicas)
        self.worker_identity = None
        self.worker_count = None
        self.aggregate_count = None
        self.extra_replacement_row = False
        self.corrupt_final = False
        self.fail_worker_close = False
        self.factory_callback = None
        self.command_callback = None
        self.factory_attempts = []

    def query(self, statement, **kwargs):
        result = super().query(statement, **kwargs)
        if statement.startswith(("SHOW CREATE TABLE ", "EXISTS TABLE ")):
            return result
        select = parse_one(statement, read=ClickHouseDialect)
        if self.aggregate_count is not None and isinstance(select, exp.Select):
            source = select.args.get("from_")
            if (
                source is not None
                and isinstance(source.this.this, exp.Anonymous)
                and select.find(exp.Count) is not None
                and "system" not in statement
            ):
                result.result_rows = self.aggregate_count
        return result

    def command(self, statement, **kwargs):
        super().command(statement, **kwargs)
        if self.command_callback is not None:
            self.command_callback(statement)
        if self.corrupt_final and statement.startswith("EXCHANGE TABLES"):
            self.corrupt_final = False
            self.host_tables[self.remote][self.source_physical].rows.append((900002,))


def install_physical_faults(monkeypatch, write_sql_connections, *, replicas=2):
    configure_independent_replicas(write_sql_connections)
    raw = PhysicalFaultClient(replicas=replicas)
    connect_pair_scope(monkeypatch, raw)

    def connect(key, endpoint, *, port=None):
        raw.factory_attempts.append((key, str(endpoint), getattr(endpoint, "tcp_port", port)))
        if raw.factory_callback is not None:
            raw.factory_callback()
        config = get_connection_config(key)
        # The actual factory applies this guard to the freshly selected config
        # before opening a host client. Keep this external boundary faithful.
        check_connection_configuration(config)
        host = next(
            name
            for name, (hostname, address, _native_port) in raw.host_endpoints.items()
            if str(endpoint) in {hostname, address}
        )
        assert getattr(endpoint, "tcp_port", port) == raw.host_endpoints[host][2]
        worker = FaultHostSession(raw, host)
        raw.worker_connections.append(worker)
        return observed_connection(worker)

    factory = import_module("analytics_toolkit.sql.connection.get_sql_connection")
    monkeypatch.setattr(factory, "get_ch_connection_for_host", connect)
    return raw


def before_physical_execution(monkeypatch, callback):
    adapter = get_backend_adapter("ch")
    execute = adapter.execute_table_reconfiguration

    def operation(connection, change, *, validate_row_count):
        callback()
        return execute(connection, change, validate_row_count=validate_row_count)

    monkeypatch.setattr(adapter, "execute_table_reconfiguration", operation)


def reselect_private_config(write_sql_connections):
    config = json.loads(get_connections_file_path().read_text())
    config["ch"]["password"] = "private-config-change-must-stay-hidden"
    set_connections_path(write_sql_connections(config))


def install_unrelated_connection(monkeypatch, write_sql_connections):
    config = json.loads(get_connections_file_path().read_text())
    config["other"] = {**config["ch"], "host": "other-host"}
    set_connections_path(write_sql_connections(config))
    opened = []

    class OtherClient:
        closed = False

        def close(self):
            self.closed = True

    def open_connection(selected, **_kwargs):
        assert selected.connection_key == "other"
        client = OtherClient()
        opened.append(client)
        return client

    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", open_connection)
    return opened


def assert_original_physical_state(raw, original):
    assert raw.snapshot() == original
    assert raw.closed
    assert all(worker.closed for worker in raw.worker_connections)


def assert_no_live_cutover(raw):
    assert all(
        not statement.startswith(("EXCHANGE TABLES", "RENAME TABLE")) for statement in raw.commands
    )
