"""Script sessions share a native catalog while retaining separate handles."""

from __future__ import annotations

from copy import deepcopy

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.ch_execute_create_scope import _ExecuteCreateConnection
from tests.sql._support.create_from_sql_protocol import (
    CatalogConnection,
    CatalogCursor,
    empty_clickhouse_catalog,
)
from tests.sql._support.execute_create_retry import RetryCreateConnection


class ScriptCursor(CatalogCursor):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ScriptConnection(CatalogConnection):
    def cursor(self):
        cursor = ScriptCursor(self)
        self.cursors.append(cursor)
        return cursor


class ScriptClickHouseClient(RetryCreateConnection):
    def __init__(self, state=None):
        state = state or empty_clickhouse_catalog()
        super().__init__(state, state.coordinator, native=False)
        prepared = parse_one(
            "CREATE TABLE sandbox.prepared (id UInt8) ENGINE=Memory", read=ClickHouseDialect
        )
        if "sandbox.prepared" not in state.host_tables[state.coordinator]:
            state._create(state.coordinator, prepared, [(7,)])

    @property
    def commands(self):
        return self.state.commands

    def command(self, statement, settings=None):
        if statement.lstrip().startswith("SET "):
            self.state.commands.append(statement)
            self.state.command_settings.append(deepcopy(settings))
            return None
        return _ExecuteCreateConnection.command(self, statement, settings=settings)

    def _metadata(self, name, hosts):
        records = super()._metadata(name, hosts)
        if name == "system.databases":
            records.extend({**row, "name": "sandbox"} for row in list(records))
        return records

    def _create_tables(self, tree, hosts):
        if tree.find(exp.TemporaryProperty) is not None and tree.find(exp.EngineProperty) is None:
            tree = tree.copy()
            properties = tree.args["properties"]
            engine = parse_one(
                "CREATE TABLE prepared (id UInt8) ENGINE=Memory", read=ClickHouseDialect
            ).find(exp.EngineProperty)
            properties.append("expressions", engine)
        return super()._create_tables(tree, hosts)
