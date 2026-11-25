"""Native creation receipts over the exact resources used by one load test."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one
from sqlparse import parse

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_replace import ReplacementClient
from tests.sql._support.ch_replace_metadata import _matches
from tests.sql._support.load_table import FakeClickHouseClient


class LoadTableCreationClient(FakeClickHouseClient):
    def __init__(self):
        super().__init__()
        self.state = ReplacementClient(storage="core", coordinator_scope="core")
        self.state.current_database = "default"
        self.state.tables.clear()
        self.state.facade_sources.clear()
        self.state.column_types.clear()

    def command(self, statement, settings=None):
        super().command(statement, settings=settings)
        tree = parse_one(strip_query_label(statement), read=ClickHouseDialect)
        for table in tree.find_all(exp.Table):
            if isinstance(table.this, exp.Identifier) and not table.db:
                table.set("db", exp.to_identifier(self.state.current_database))
        if isinstance(tree, exp.Create):
            for engine in tree.find_all(exp.EngineProperty):
                if engine.this.name.lower() == "distributed":
                    arguments = engine.this.expressions
                    if not isinstance(arguments[1], exp.Literal):
                        arguments[1] = exp.Literal.string(self.state.current_database)
        self.state.command(tree.sql(dialect=ClickHouseDialect), settings=settings)

    def query(self, statement, settings=None, **kwargs):
        self.queries.append(statement)
        body = strip_query_label(statement)
        if body.startswith("EXISTS TABLE "):
            table = parse_one(body[len("EXISTS TABLE ") :], read=ClickHouseDialect, into=exp.Table)
            name = f"{table.db or self.state.current_database}.{table.name}"
            return SimpleNamespace(result_rows=[(int(("core", name) in self.state.tables),)])
        tree = (
            parse_one(statement, read=ClickHouseDialect)
            if parse(statement)[0].get_type() == "SELECT"
            else None
        )
        if isinstance(tree, exp.Select) and tree.find(exp.Count) is not None:
            scope, table = self.state.reference(tree.args["from_"].this)
            if table == "system.columns":
                records = [
                    {**record, "currentDatabase": self.state.current_database}
                    for record in self.state.metadata._column_records(scope)
                ]
                condition = tree.args.get("where")
                if condition is not None:
                    records = [record for record in records if _matches(condition.this, record)]
                return SimpleNamespace(result_rows=[(len(records),)])
        if isinstance(tree, exp.Select):
            qualified = False
            for table in tree.find_all(exp.Table):
                if isinstance(table.this, exp.Identifier) and not table.db:
                    table.set("db", exp.to_identifier(self.state.current_database))
                    qualified = True
            if qualified:
                statement = tree.sql(dialect=ClickHouseDialect)
        return self.state.query(statement, settings=settings, **kwargs)

    def _record_rows(self, table, size):
        reference = parse_one(f"SELECT * FROM {table}", read=ClickHouseDialect).find(exp.Table)
        if not reference.db:
            reference.set("db", exp.to_identifier(self.state.current_database))
        name = self.state.table_name(reference)
        identity = ("core", name)
        identity = self.state.facade_sources.get(identity, identity)
        resource = self.state.tables[identity]
        self.state.tables[identity] = replace(resource, rows=resource.rows + size)

    def insert_df(self, table, df, column_names, **kwargs):
        super().insert_df(table, df, column_names)
        self._record_rows(table, len(df))

    def insert(self, table, data, column_names, column_type_names=None, **kwargs):
        rows = list(data)
        super().insert(table, rows, column_names, column_type_names)
        self._record_rows(table, len(rows))
