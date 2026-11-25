"""CTAS endpoint state with real lifecycle ownership and population faults."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy

import pandas as pd
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.backends.ch.outage_context import INTERNAL_IO
from analytics_toolkit.sql.execution.observation import observed_connection
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_execute_create_scope import (
    _ExecuteCreateConnection,
    install_execute_create_scope,
)
from tests.sql._support.ch_transfer_replacement import _Result
from tests.sql._support.reconfigure_conversion_scope import _name
from tests.sql.backends.ch._support.exact_transport import native_context


class RetryCreateConnection(_ExecuteCreateConnection):
    def __init__(self, state, host, *, native):
        super().__init__(state, host, native=native)
        if native:
            self.context = native_context()

    def query(self, statement, settings=None, **kwargs):
        body = strip_query_label(statement)
        if body.startswith("SELECT toTypeName(defaultValueOfTypeName("):
            declarations = [
                item.this for item in parse_one(body, read=ClickHouseDialect).find_all(exp.Literal)
            ]
            return _Result(
                [tuple("Int64" if native == "BIGINT" else native for native in declarations)],
                ["type"] * len(declarations),
            )
        if body.startswith("DESCRIBE TABLE ("):
            query = parse_one(body[len("DESCRIBE TABLE ") :], read=ClickHouseDialect)
            while isinstance(query, (exp.Paren, exp.Subquery)):
                query = query.this
            columns = self._source_columns(query)
            return _Result(
                [
                    (column.name, column.kind.sql(dialect=ClickHouseDialect), "")
                    for column in columns
                ],
                ["name", "type", "default_type"],
            )
        return super().query(statement, settings=settings, **kwargs)

    def _source_columns(self, query):
        source = query.args.get("from_")
        if source is None:
            columns = []
            for projection in query.expressions:
                value = projection.unalias()
                native = (
                    value.args["to"].sql(dialect=ClickHouseDialect)
                    if isinstance(value, exp.Cast)
                    else "UInt8"
                )
                columns.append(
                    exp.ColumnDef(
                        this=exp.to_identifier(projection.alias_or_name),
                        kind=exp.DataType.build(native, dialect=ClickHouseDialect),
                    )
                )
            return columns
        hosts, name = self._reference(source.this)
        schema = self.state.host_tables[hosts[0]][name].schema
        return [
            exp.ColumnDef(
                this=exp.to_identifier(projection.alias_or_name),
                kind=exp.DataType.build(
                    schema[projection.unalias().name], dialect=ClickHouseDialect
                ),
            )
            for projection in query.expressions
        ]

    def _create_tables(self, tree, hosts):
        source = tree.expression
        if (
            isinstance(tree.this, exp.Schema)
            or not isinstance(source, exp.Select)
            or source.args.get("from_") is None
        ):
            return super()._create_tables(tree, hosts)
        target = tree.this
        name = _name(target)
        pending = [host for host in hosts if name not in self.state.host_tables[host]]
        if not pending and tree.args.get("exists"):
            return None
        assert len(pending) == len(hosts), "Plain CREATE must not adopt a collision."
        # Native CTAS evaluates its actual source now. EMPTY still derives its
        # schema from the source, but does not populate the created storage.
        columns = self._source_columns(source)
        selected = self._select(source).result_rows
        rows = [] if tree.find(exp.EmptyProperty) is not None else selected
        created = tree.copy()
        created.set("this", exp.Schema(this=target.copy(), expressions=columns))
        created.set("expression", None)
        properties = created.args.get("properties")
        if properties is not None:
            properties.set(
                "expressions",
                [
                    item
                    for item in properties.expressions
                    if not isinstance(item, exp.EmptyProperty)
                ],
            )
        for host in pending:
            self.state._create(host, created, rows)
        self._written_rows = len(rows) * len(pending)
        return None

    @contextmanager
    def query_df_stream(self, statement, *, settings=None, **_kwargs):
        self.state.queries.append(statement)
        self.state.query_settings.append(deepcopy(settings))
        selected = self._select(parse_one(strip_query_label(statement), read=ClickHouseDialect))
        self.state.source_scans.append((statement, deepcopy(selected.result_rows)))
        assert len(self.state.source_scans) == 1
        self.state.events.append("scan")
        frame = pd.DataFrame(
            {"id": pd.Series([row[0] for row in selected.result_rows], dtype=object)}
        )

        def blocks():
            yield frame
            self.state.events.append("input_complete")

        yield blocks()

    def command(self, statement, settings=None):
        body = strip_query_label(statement)
        if body.startswith(("DROP TABLE", "CREATE TABLE")):
            self.state.events.append("mutation")
        if body.startswith("INSERT INTO analytics.retry_events"):
            self.state.population_attempts.append((self, statement))
            snapshot = self.state.snapshot()
            owned = {
                (host, name): value
                for host, tables in snapshot.items()
                for name, value in tables.items()
                if name in {"analytics.retry_events", "analytics.retry_events_shard"}
            }
            self.state.population_layouts.append(owned)
            if self.state.population_failures:
                self.state.population_failures -= 1
                self.state.empty_before_population = all(
                    value["rows"] == [] for value in owned.values()
                )
                if self.state.failure_mode == "lost-ack":
                    super().command(statement, settings=settings)
                else:
                    self.state.commands.append(statement)
                    self.state.command_settings.append(deepcopy(settings))
                    self.state.host_commands[self.host].append(statement)
                raise self.state.population_error
        return super().command(statement, settings=settings)


def execute_create_retry_target(monkeypatch, write_sql_connections, *, transport, local=False):
    def configure(connections):
        configured = deepcopy(connections)
        configured["ch"].update(
            driver=transport,
            port=9000 if transport == "native" else 8123,
            transfer_staging_schema=None,
        )
        return write_sql_connections(configured)

    state = install_execute_create_scope(monkeypatch, configure, local_policy=local)
    state.population_attempts = []
    state.population_layouts = []
    state.population_failures = 0
    state.population_error = ConnectionError("Final native population acknowledgment was lost")
    state.failure_mode = None
    state.empty_before_population = False
    state.source_scans = []
    state.events = []

    def connect(config, **_kwargs):
        matches = [
            host
            for host, (name, address, _port) in state.host_endpoints.items()
            if config.host in {host, name, address}
        ]
        assert len(matches) == 1
        raw = RetryCreateConnection(state, matches[0], native=config.driver == "native")
        state.opened.append(raw)
        state.opened_configs.append(config)
        if INTERNAL_IO.get():
            return raw
        wrapped = OutageClient(observed_connection(raw), config)
        state.wrappers.append(wrapped)
        return wrapped

    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", connect)
    return state
