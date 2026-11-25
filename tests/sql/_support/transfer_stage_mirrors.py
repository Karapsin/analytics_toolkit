"""Real transfer stages on B with individually visible coordinator-A mirrors."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.backends.ch.outage_context import INTERNAL_IO
from analytics_toolkit.sql.execution.observation import observed_connection
from psycopg2 import extensions
from sqlglot import exp, parse_one

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.ch_drop_scope import DropScopeState
from tests.sql._support.ch_lifecycle import LifecycleConnection
from tests.sql._support.ch_transfer_replacement import _Result
from tests.sql._support.outage_gp import Connection, Cursor, MemoryGreenplum
from tests.sql.backends.ch._support.exact_transport import native_context
from tests.sql.backends.ch._support.network_transport import encode_normalized


class MirrorConnection(LifecycleConnection):
    def __init__(self, state, host, *, native):
        super().__init__(state, host, native=native)
        if native:
            self.context = native_context()

    def query(self, statement, settings=None, **kwargs):
        if statement.startswith("SELECT toTypeName(defaultValueOfTypeName("):
            declarations = [
                item.this
                for item in parse_one(statement, read=ClickHouseDialect).find_all(exp.Literal)
            ]
            return _Result([tuple(declarations)], ["type"] * len(declarations))
        return super().query(statement, settings=settings, **kwargs)

    def _record_wire(self, rows):
        copied = [tuple(row) for row in rows]
        self.state.client_rows.append(copied)
        self.state.client_wire.append(
            encode_normalized("UInt64", [row[0] for row in copied], self.state.transport)
        )
        return copied

    def insert(self, table, data, column_names, **kwargs):
        return super().insert(table, self._record_wire(data), column_names, **kwargs)

    def data_insert(self, context):
        context.data = self._record_wire(context.data)
        return super().data_insert(context)


def mirror_transfer_target(monkeypatch, write_sql_connections, transport):
    configs = deepcopy(DEFAULT_SQL_CONNECTIONS)
    configs["gp"]["transfer_staging_schema"] = None
    configs["ch"].update(
        driver=transport,
        port=9000 if transport == "native" else 8123,
        host="coordinator",
        database="analytics",
        transfer_staging_schema="analytics",
        ddl_defaults={
            "regular": {
                "create_distributed_pair": True,
                "shard": {"engine": "MergeTree", "on_cluster": "storage"},
                "distributed": {
                    "cluster": "storage",
                    "on_cluster": None,
                    "sharding_key": "rand()",
                    "engine_template": (
                        "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                    ),
                },
            },
            "staging": {
                "create_distributed_pair": True,
                "shard": {"engine": "MergeTree", "on_cluster": "storage"},
                "distributed": {
                    "cluster": "storage",
                    "on_cluster": "storage",
                    "sharding_key": "rand()",
                    "engine_template": (
                        "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                    ),
                },
            },
        },
    )
    write_sql_connections(configs)
    source = MemoryGreenplum()
    source.tables["db.source"] = pd.DataFrame({"id": [201, 202]})
    state = DropScopeState()
    state.transport = transport
    state.client_rows = []
    state.client_wire = []
    state.created.clear()

    def register_type(_caster, cursor):
        assert isinstance(cursor, Cursor)

    def connect_ch(config, **_kwargs):
        matches = [
            host
            for host, (name, address, _port) in state.host_endpoints.items()
            if config.host in {host, name, address}
        ]
        assert len(matches) == 1
        raw = MirrorConnection(state, matches[0], native=config.driver == "native")
        state.opened.append(raw)
        state.opened_configs.append(config)
        if INTERNAL_IO.get():
            return raw
        wrapped = OutageClient(observed_connection(raw), config)
        state.wrappers.append(wrapped)
        return wrapped

    monkeypatch.setattr(extensions, "register_type", register_type)
    monkeypatch.setattr(
        get_backend_adapter("gp"), "open_connection", lambda *_args, **_kwargs: Connection(source)
    )
    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", connect_ch)
    return source, state
