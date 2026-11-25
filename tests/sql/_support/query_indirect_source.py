"""Native view/facade dependencies at stateful query-creation boundaries."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy

import pandas as pd
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.backends.ch.outage_context import INTERNAL_IO
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.execution.observation import observed_connection
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import install_drop_scope
from tests.sql._support.ch_lifecycle import LifecycleConnection
from tests.sql._support.ch_transfer_replacement import _Result
from tests.sql._support.query_replay_options import (
    ReplayTrinoConnection,
    ReplayTrinoCursor,
    ReplayTrinoDatabase,
    _connections,
)
from tests.sql._support.trino_parameters import DriverParameterCursor
from tests.sql.backends.ch._support.exact_transport import native_context
from tests.sql.backends.ch._support.network_transport import encode_normalized


class IndirectChConnection(LifecycleConnection):
    def __init__(self, state, host, *, native):
        super().__init__(state, host, native=native)
        self.context = native_context()

    def query(self, statement, settings=None, **kwargs):
        body = strip_query_label(statement)
        if body.startswith("DESCRIBE TABLE ("):
            self.state.queries.append(statement)
            return _Result([("id", "Int64", "")], ["name", "type", "default_type"])
        if body.startswith("SELECT toTypeName(defaultValueOfTypeName("):
            declarations = [
                item.this for item in parse_one(body, read=ClickHouseDialect).find_all(exp.Literal)
            ]
            return _Result([tuple(declarations)], ["type"] * len(declarations))
        return super().query(statement, settings=settings, **kwargs)

    @contextmanager
    def query_df_stream(self, statement, **kwargs):
        self.state.source_stream_settings = deepcopy(kwargs.get("settings"))
        result = self._select(parse_one(strip_query_label(statement), read=ClickHouseDialect))
        self.state.source_scans.append((statement, deepcopy(result.result_rows)))
        assert len(self.state.source_scans) == 1, "Indirect source was queried after certification."
        self.state.source_events.append("scan")
        frame = pd.DataFrame(
            {"id": pd.Series([row[0] for row in result.result_rows], dtype=object)}
        )

        def blocks():
            yield frame
            self.state.source_events.append("input_complete")

        yield blocks()

    def command(self, statement, settings=None):
        body = strip_query_label(statement)
        if body.startswith(("DROP TABLE", "CREATE TABLE")):
            self.state.source_events.append("mutation")
        return super().command(statement, settings=settings)

    def insert(self, table, data, column_names, **kwargs):
        rows = list(data)
        self.state.native_wire.append(
            encode_normalized("Int64", [row[0] for row in rows], "native")
        )
        return super().insert(table, rows, column_names, **kwargs)


def indirect_ch_pair(monkeypatch, write_sql_connections):
    state = install_drop_scope(monkeypatch, write_sql_connections, driver="native")
    state.source_scans = []
    state.source_events = []
    state.native_wire = []
    physical = parse_create_table(
        "CREATE TABLE analytics.events_shard (id Int64) ENGINE=MergeTree ORDER BY id",
        "analytics.events_shard",
    )
    for host, rows in ((state.coordinator, state.foreign_rows), (state.remote, [(1,), (2,)])):
        state.host_tables[host].pop("analytics.events_shard")
        state._create(host, physical.copy(), rows)
    state.host_tables[state.coordinator].pop("analytics.events")
    for relation in ("events", "events_alias"):
        facade = parse_create_table(
            f"CREATE TABLE analytics.{relation} (id Int64) "
            "ENGINE=Distributed('storage', 'analytics', 'events_shard', rand())",
            f"analytics.{relation}",
        )
        state._create(state.coordinator, facade)
    state.created.clear()

    def connect(config, **_kwargs):
        matches = [
            host
            for host, (name, address, port) in state.host_endpoints.items()
            if config.host in {host, name, address} and config.port == port
        ]
        assert len(matches) == 1
        raw = IndirectChConnection(state, matches[0], native=True)
        state.opened.append(raw)
        state.opened_configs.append(config)
        if INTERNAL_IO.get():
            return raw
        wrapped = OutageClient(observed_connection(raw), config)
        state.wrappers.append(wrapped)
        return wrapped

    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", connect)
    return state


class IndirectTrinoCursor(ReplayTrinoCursor):
    def execute(self, statement, values=None):
        body = strip_query_label(statement)
        database = self.connection.database
        if body.upper().startswith("SHOW CREATE VIEW "):
            DriverParameterCursor.execute(self, statement, values)
            self.rows = [
                (
                    "CREATE VIEW iceberg.sandbox.events_view SECURITY DEFINER AS "
                    + database.view_definition,
                )
            ]
            self.description = [("Create View", "VARCHAR", None, None, None, None, None)]
            database.view_metadata.append(statement)
            return self
        tree = parse_one(body, read="trino")
        table = tree.find(exp.Table)
        if (
            isinstance(tree, exp.Select)
            and table is not None
            and table.db == "information_schema"
            and table.name == "views"
        ):
            DriverParameterCursor.execute(self, statement, values)
            record = {
                "table_catalog": "iceberg",
                "table_schema": "sandbox",
                "table_name": "events_view",
                "view_definition": database.view_definition,
            }
            self.rows = [tuple(record[item.alias_or_name] for item in tree.expressions)]
            self.description = [
                (item.alias_or_name, "VARCHAR", None, None, None, None, None)
                for item in tree.expressions
            ]
            database.view_metadata.append(statement)
            return self
        if isinstance(tree, exp.Insert) and isinstance(tree.expression, exp.Select):
            DriverParameterCursor.execute(self, statement, values)
            source = tree.expression.find(exp.Table)
            assert source.name == "events_view"
            assert database.exists
            database.server_copies.append(deepcopy(database.rows))
            database.rows.extend(deepcopy(database.rows))
            database.events.append("server_copy")
            self.rowcount = len(database.server_copies[-1])
            return self
        return super().execute(statement, values)


class IndirectTrinoConnection(ReplayTrinoConnection):
    def cursor(self):
        return IndirectTrinoCursor(self)


def indirect_trino_view(monkeypatch, write_sql_connections, tmp_path):
    _connections(monkeypatch, write_sql_connections, tmp_path)
    database = ReplayTrinoDatabase([(1,), (2,)], "BIGINT")
    database.view_definition = "SELECT id FROM iceberg.sandbox.events"
    database.view_metadata = []
    database.server_copies = []

    def connect(_config, **_kwargs):
        client = IndirectTrinoConnection(database)
        database.opened.append(client)
        return client

    monkeypatch.setattr(get_backend_adapter("trino"), "open_connection", connect)
    return database
