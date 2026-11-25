"""Independent final INSERT and cleanup faults after a real owned CREATE."""

from __future__ import annotations

from copy import deepcopy

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.backends.ch.outage_context import INTERNAL_IO
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.execution.observation import observed_connection
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_transfer_replacement import _parse_command
from tests.sql._support.outage_gp import Connection, Cursor
from tests.sql._support.reconfigure_conversion_scope import _name, _scope_sql
from tests.sql._support.transfer_stage_mirrors import (
    MirrorConnection,
    mirror_transfer_target,
)

TARGET = "analytics.events"
CLEANUP_SECRET = "private-cleanup-driver-payload-201202"


class PrimaryInsertError(RuntimeError):
    code = 53


class PrimarySourceCursor(Cursor):
    def __init__(self, store, state):
        super().__init__(store)
        self.state = state
        self.selected = False

    def execute(self, statement, *args):
        tree = parse_one(statement, read="postgres")
        self.selected = (
            isinstance(tree, exp.Select)
            and tree.find(exp.Count) is None
            and any(table.name == "source" for table in tree.find_all(exp.Table))
            and not any(node.alias == "source_schema_probe" for node in tree.find_all(exp.Subquery))
        )
        return super().execute(statement, *args)

    def fetchmany(self, size):
        rows = super().fetchmany(size)
        if self.selected and rows:
            self.state.source_batches.append(rows)
        return rows


class PrimarySourceConnection(Connection):
    def __init__(self, source, state):
        super().__init__(source)
        self.state = state
        self.close_calls = 0

    def cursor(self, *_args, **_kwargs):
        return PrimarySourceCursor(self.store, self.state)

    def close(self):
        self.close_calls += 1
        super().close()


class PrimaryFailureConnection(MirrorConnection):
    def command(self, statement, settings=None):
        _cluster, body = _scope_sql(strip_query_label(statement))
        if body.lstrip().upper().startswith(("INSERT ", "DROP ")):
            tree = _parse_command(body)
            target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
            if isinstance(tree, exp.Drop):
                targets = list(tree.find_all(exp.Table))
                assert len(targets) == 1
                target = targets[0]
            if isinstance(tree, exp.Insert) and _name(target) == TARGET:
                self.state.population_attempts.append((self.host, statement))
                owned = self.state.host_tables[self.host][TARGET]
                self.state.acknowledged_target_uuid = owned.uuid
                assert owned.rows == []
                if self.state.cleanup_mode == "replacement":
                    self.state.host_tables[self.host].pop(TARGET)
                    replacement = parse_create_table(
                        "CREATE TABLE analytics.events (id UInt64) ENGINE=MergeTree ORDER BY id",
                        TARGET,
                    )
                    self.state._create(self.host, replacement, [(value,) for value in range(100)])
                    self.state.replacement = self.state.snapshot()[self.host][TARGET]
                raise self.state.primary_error
            if isinstance(tree, exp.Drop) and _name(target) == TARGET:
                self.state.target_cleanup_attempts.append((self.host, statement))
                assert self.state.cleanup_mode == "drop_failure"
                raise self.state.cleanup_error
        return super().command(statement, settings=settings)


def target_primary_failure(monkeypatch, write_sql_connections, transport, cleanup_mode):
    def local_connections(configs):
        configs = deepcopy(configs)
        for purpose in ("regular", "staging"):
            configs["ch"]["ddl_defaults"][purpose] = {
                "create_distributed_pair": False,
                "shard": {"engine": "MergeTree", "on_cluster": None},
            }
        return write_sql_connections(configs)

    source, state = mirror_transfer_target(monkeypatch, local_connections, transport)
    state.host_tables[state.coordinator].pop(TARGET)
    state.cleanup_mode = cleanup_mode
    state.primary_error = PrimaryInsertError("Final native INSERT rejected its destination type.")
    state.cleanup_error = PermissionError(CLEANUP_SECRET)
    state.population_attempts = []
    state.target_cleanup_attempts = []
    state.source_batches = []
    state.source_connections = []
    state.acknowledged_target_uuid = None
    state.replacement = None

    def connect_source(*_args, **_kwargs):
        connection = PrimarySourceConnection(source, state)
        state.source_connections.append(connection)
        return connection

    def connect_target(config, **_kwargs):
        matches = [
            host
            for host, (name, address, _port) in state.host_endpoints.items()
            if config.host in {host, name, address}
        ]
        assert len(matches) == 1
        raw = PrimaryFailureConnection(state, matches[0], native=config.driver == "native")
        state.opened.append(raw)
        state.opened_configs.append(config)
        if INTERNAL_IO.get():
            return raw
        wrapped = OutageClient(observed_connection(raw), config)
        state.wrappers.append(wrapped)
        return wrapped

    monkeypatch.setattr(get_backend_adapter("gp"), "open_connection", connect_source)
    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", connect_target)
    return source, state
