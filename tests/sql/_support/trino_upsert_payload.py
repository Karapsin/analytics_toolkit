"""Actual Trino load SQL over a typed, partitioned in-memory driver boundary."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

from analytics_toolkit.sql.connection.config import get_connection_config
from sqlglot import exp, parse_one

from tests.sql._support.load_table import load_df_module
from tests.sql._support.transfer_not_null import (
    TransferConnection,
    TransferCursor,
    TransferDatabase,
)
from tests.sql._support.trino_parameters import render_parameter


def _wall_timestamp(year, month, day, *, microsecond):
    return dt.datetime.combine(dt.date(year, month, day), dt.time(microsecond=microsecond))


SCHEMA = {"id": "BIGINT", "month": "VARCHAR", "value": "TIMESTAMP(6)"}
SEED = [
    {"id": 1, "month": "Jan", "value": _wall_timestamp(2025, 1, 1, microsecond=111111)},
    {"id": 2, "month": "Jan", "value": _wall_timestamp(2025, 1, 1, microsecond=123789)},
    {"id": 3, "month": "Feb", "value": _wall_timestamp(2025, 1, 1, microsecond=999999)},
]
INCOMING = {"id": 1, "month": "Jan", "value": _wall_timestamp(2025, 1, 2, microsecond=456000)}


def _assign(value, native):
    if value is None:
        return None
    kind = exp.DataType.build(native, dialect="trino")
    if kind.this == exp.DataType.Type.TIMESTAMP:
        value = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
        assert isinstance(value, dt.datetime)
        assert value.tzinfo is None
        precision = int(kind.expressions[0].this.name) if kind.expressions else 3
        if precision < 6:
            factor = 10 ** (6 - precision)
            micros = (value.microsecond + factor // 2) // factor * factor
            value += dt.timedelta(microseconds=micros - value.microsecond)
        return value
    if kind.this == exp.DataType.Type.BIGINT:
        assert isinstance(value, int)
        return value
    assert kind.this == exp.DataType.Type.VARCHAR
    assert isinstance(value, str)
    return value


class UpsertDatabase(TransferDatabase):
    def __init__(self):
        super().__init__("trino", columns=tuple(SCHEMA), target_types=SCHEMA, not_null=())
        self.target.rows = deepcopy(SEED)
        self.opened = []
        self.formatted = []
        self.created_schemas = []
        self.publications = []
        self.partition_drops = []
        self.publication_error = None
        self.publication_callback = None
        self.fail_after_ack = False

    def execute(self, connection, statement, params=None):
        # Metadata remains the actual table/column state supplied to the adapter.
        if "information_schema" in statement:
            return super().execute(connection, statement, params)
        tree = parse_one(statement, read="trino")
        table = tree.find(exp.Table)
        identity = self.identity(table) if table is not None else None
        publishing = isinstance(tree, exp.Insert) and identity == ("sandbox", "events")
        if publishing:
            self.publications.append(statement)
            if self.publication_error is not None and not self.fail_after_ack:
                error, self.publication_error = self.publication_error, None
                if self.publication_callback is not None:
                    self.publication_callback()
                raise error
        result = super().execute(connection, statement, params)
        if isinstance(tree, exp.Create):
            self.created_schemas.append((identity, dict(self.tables[identity].schema)))
        if isinstance(tree, exp.Delete) and identity == ("sandbox", "events"):
            self.partition_drops.append(statement)
        if publishing and self.publication_error is not None:
            error, self.publication_error = self.publication_error, None
            if self.publication_callback is not None:
                self.publication_callback()
            raise error
        return result

    def value(self, node, row):
        if isinstance(node, exp.Cast):
            return _assign(self.value(node.this, row), node.args["to"].sql(dialect="trino"))
        return super().value(node, row)

    def _insert(self, tree, identity, params):
        candidate = deepcopy(self.tables[identity])
        names = [column.name for column in tree.this.expressions] or list(candidate.schema)
        if isinstance(tree.expression, exp.Values):
            width = len(names)
            values = [params[index : index + width] for index in range(0, len(params), width)]
        else:
            assert params is None
            values, _columns = self.select(tree.expression)
        rows = [
            tuple(_assign(value, candidate.schema[name]) for name, value in zip(names, row))
            for row in values
        ]
        candidate.insert(names, rows)
        self.tables[identity] = candidate
        if identity != ("sandbox", "events"):
            self.stage_inserts.append((identity, deepcopy(rows)))


class _UpsertCursor(TransferCursor):
    def execute(self, statement, params=None):
        if params:
            wire = [render_parameter(value) for value in params]
            self.connection.database.formatted.append((statement, tuple(params), wire))
        return super().execute(statement, params)


class _UpsertConnection(TransferConnection):
    def __init__(self, database):
        super().__init__(database)
        self.close_calls = 0

    def cursor(self):
        return _UpsertCursor(self)

    def close(self):
        self.close_calls += 1


def configure_upsert(monkeypatch, write_sql_connections):
    configuration = {
        "trino": {
            "type": "trino",
            "host": "first.invalid",
            "port": 8080,
            "user": "fake",
            "password": "fake",
            "catalog": "iceberg",
            "schema": "sandbox",
            "upsert_partition_drop_sql_template": (
                "DELETE FROM {table} WHERE {partition_column} "
                "IS NOT DISTINCT FROM {partition_value}"
            ),
        }
    }
    write_sql_connections(configuration)
    first, second = UpsertDatabase(), UpsertDatabase()
    second.target.rows = [
        {"id": 91, "month": "Jan", "value": _wall_timestamp(2024, 1, 1, microsecond=987654)}
    ]

    def connect(alias):
        database = first if get_connection_config(alias).host == "first.invalid" else second
        connection = _UpsertConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return first, second, configuration
