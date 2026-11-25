"""Stateful ownership faults at actual query-creation transport boundaries."""

from __future__ import annotations

from contextlib import contextmanager
from copy import copy
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.adaptive_snapshot_scope import _value
from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_execute_create_scope import _literal_type
from tests.sql._support.ch_transfer_replacement import _Result
from tests.sql._support.create_from_sql import create_module
from tests.sql._support.create_from_sql_protocol import CatalogClickHouseClient
from tests.sql._support.reconfigure_conversion_scope import _name, _scope_sql


class QueryOwnershipClient(CatalogClickHouseClient):
    def __init__(self, failure=None):
        super().__init__()
        self.fault = {"kind": failure, "fired": False, "foreign": []}
        source = parse_one(
            "CREATE TABLE analytics.source_table (id UInt64, amount Decimal(12,2)) "
            "ENGINE=MergeTree ORDER BY id",
            read=ClickHouseDialect,
        )
        self.state._create(self.host, source, [(1, Decimal("1.25"))])
        self.state.created.clear()

    def _query_schema(self, query):
        source = query.args.get("from_")
        if source is None:
            inputs = {}
        elif isinstance(source.this, exp.Subquery):
            inputs = self._query_schema(source.this.this)
        else:
            hosts, name = self._reference(source.this)
            inputs = self.state.host_tables[hosts[0]][name].schema
        result = {}
        for projection in query.expressions:
            value = projection.unalias()
            if value.is_star:
                result.update(inputs)
            elif isinstance(value, exp.Column):
                result[projection.alias_or_name] = inputs[value.name]
            elif isinstance(value, exp.Cast):
                result[projection.alias_or_name] = value.args["to"].sql(dialect=ClickHouseDialect)
            elif isinstance(value, exp.Literal):
                result[projection.alias_or_name] = _literal_type(value)
            else:
                assert isinstance(value, exp.Window)
                assert isinstance(value.this, exp.RowNumber)
                result[projection.alias_or_name] = "UInt64"
        return result

    def _select(self, select):
        if not isinstance(select, exp.Select) or not any(
            isinstance(item.unalias(), exp.Window) for item in select.expressions
        ):
            return super()._select(select)
        records, names = self._source(select)
        expressions = [
            field
            for expression in select.expressions
            for field in (
                [exp.column(name) for name in names] if expression.is_star else [expression]
            )
        ]
        rows = [
            tuple(_value(expression, record, ordinal, len(records)) for expression in expressions)
            for ordinal, record in enumerate(records, 1)
        ]
        return _Result(rows, [expression.alias_or_name for expression in expressions])

    def _create_tables(self, tree, hosts):
        if tree.args.get("replace"):
            target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
            for host in hosts:
                self.state.host_tables[host].pop(_name(target), None)
        if not isinstance(tree.this, exp.Table) or not isinstance(tree.expression, exp.Select):
            return super()._create_tables(tree, hosts)
        schema = self._query_schema(tree.expression)
        rows = (
            []
            if tree.find(exp.EmptyProperty) is not None
            else self._select(tree.expression).result_rows
        )
        created = tree.copy()
        created.set(
            "this",
            exp.Schema(
                this=tree.this.copy(),
                expressions=[
                    exp.ColumnDef(
                        this=exp.to_identifier(name),
                        kind=exp.DataType.build(native, dialect=ClickHouseDialect),
                    )
                    for name, native in schema.items()
                ],
            ),
        )
        created.set("expression", None)
        super()._create_tables(created, hosts)
        for host in hosts:
            self.state.host_tables[host][_name(tree.this)].rows.extend(rows)
        return None

    def query(self, statement, settings=None, **kwargs):
        body = strip_query_label(statement)
        if body.startswith("DESCRIBE TABLE ("):
            query = parse_one(body[len("DESCRIBE TABLE ") :], read=ClickHouseDialect)
            while isinstance(query, (exp.Subquery, exp.Paren)):
                query = query.this
            return SimpleNamespace(
                result_rows=[(name, native) for name, native in self._query_schema(query).items()]
            )
        result = super().query(statement, settings=settings, **kwargs)
        if result.column_names and all(
            name in {"id", "amount"} or name.startswith("__analytics_toolkit_")
            for name in result.column_names
        ):
            query = parse_one(body, read=ClickHouseDialect)
            result.column_types = tuple(self._query_schema(query).values())
        return result

    @contextmanager
    def query_df_stream(self, statement, *, settings=None, **kwargs):
        result = self.query(statement, settings=settings, **kwargs)
        frame = pd.DataFrame(
            {
                name: pd.Series(values, dtype=object)
                for name, values in zip(result.column_names, result.result_columns)
            }
        )
        yield iter((frame,))

    def command(self, statement, settings=None):
        cluster, body = _scope_sql(strip_query_label(statement))
        tree = parse_one(body, read=ClickHouseDialect)
        table = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        if (
            isinstance(tree, exp.Create)
            and isinstance(table, exp.Table)
            and _name(table) == "analytics.events"
            and self.fault["kind"] is not None
            and not self.fault["fired"]
        ):
            self.fault["fired"] = True
            if self.fault["kind"] == "collision":
                self.state.commands.append(statement)
                foreign = parse_one(
                    "CREATE TABLE analytics.events (id UInt64, amount Decimal(12,2)) "
                    "ENGINE=MergeTree ORDER BY id",
                    read=ClickHouseDialect,
                )
                hosts = self.state.clusters[cluster] if cluster is not None else (self.host,)
                for host in hosts:
                    self.state._create(
                        host, foreign, [(number, Decimal(number)) for number in range(100)]
                    )
                    resource = self.state.host_tables[host]["analytics.events"]
                    self.fault["foreign"].append(
                        (host, resource, resource.uuid, resource.create.copy())
                    )
                error = RuntimeError("concurrent facade TABLE_ALREADY_EXISTS")
                error.error_name = "TABLE_ALREADY_EXISTS"
                raise error
            super().command(statement, settings=settings)
            message = "facade CREATE acknowledgement lost"
            raise ConnectionError(message)
        return super().command(statement, settings=settings)

    def seed_existing_pair(self):
        physical = parse_one(
            "CREATE TABLE analytics.events_shard (id UInt64, amount Decimal(12,2)) "
            "ENGINE=MergeTree ORDER BY id",
            read=ClickHouseDialect,
        )
        facade = parse_one(
            "CREATE TABLE analytics.events (id UInt64, amount Decimal(12,2)) "
            "ENGINE=Distributed('storage','analytics','events_shard',rand())",
            read=ClickHouseDialect,
        )
        for host in (self.state.coordinator, self.state.remote):
            self.state._create(host, physical, [(number, Decimal(number)) for number in range(100)])
            self.state._create(host, facade)
        self.state.created.clear()


def install_query_ownership_client(monkeypatch, client, *, source=None):
    def acquire(key):
        if key != "ch" and source is not None:
            return source
        connection = copy(client)
        connection.closed = False
        return connection

    monkeypatch.setattr(create_module, "get_sql_connection", acquire)
    monkeypatch.setattr(
        "analytics_toolkit.sql.backends.ch.create_table_as.get_sql_connection", acquire
    )
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.runtime.retry.get_sql_connection", acquire
    )
