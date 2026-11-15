"""Native DATE assignment over real psycopg2 adaptation and VALUES formatting."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

from sqlglot import exp, parse_one

from tests.sql._support.gp_temporal_values import (
    TemporalConnection,
    TemporalCursor,
    TemporalDatabase,
)
from tests.sql._support.load_table import load_df_module


class DateDatabase(TemporalDatabase):
    def __init__(self):
        super().__init__("DATE")
        self.target.rows = [{"payload": dt.date(2001, 1, 2)}, {"payload": dt.date(2002, 3, 4)}]
        self.old_rows = deepcopy(self.target.rows)
        self.opened = []
        self.formatter_observations = []

    @property
    def schema(self):
        return self.target.schema

    @property
    def rows(self):
        return self.target.rows

    @property
    def statements(self):
        return self.commands

    @property
    def writes(self):
        return self.live_mutations

    @property
    def submissions(self):
        result = []
        for statement in self.writes:
            tree = parse_one(self._ddl_schema_prefix(statement), read="postgres")
            kind = (
                "CLEAR"
                if isinstance(tree, (exp.Delete, exp.TruncateTable))
                else "INSERT"
                if isinstance(tree, exp.Insert)
                else "DDL"
            )
            result.append((kind, statement))
        return result

    @staticmethod
    def decode_value(node, native_type):
        assert native_type.upper() == "DATE"
        timestamp = False
        while isinstance(node, exp.Cast):
            requested = node.args["to"].sql(dialect="postgres").upper()
            if "TIMESTAMP" in requested:
                assert "WITH TIME ZONE" not in requested
                assert "TIMESTAMPTZ" not in requested
                timestamp = True
            node = node.this
        if isinstance(node, exp.Null):
            return None
        assert isinstance(node, exp.Literal)
        assert node.is_string
        if timestamp:
            value = dt.datetime.fromisoformat(node.this)
            assert value.tzinfo is None
            return value.date()
        return dt.date.fromisoformat(node.this)


class _DateConnection(TemporalConnection):
    def __init__(self, database):
        super().__init__(database)
        self.close_calls = 0

    def cursor(self):
        return _DateCursor(self)

    def close(self):
        self.close_calls += 1


class _DateCursor(TemporalCursor):
    def mogrify(self, template, values):
        wire = super().mogrify(template, values)
        self.connection.database.formatter_observations.append((template, tuple(values), wire))
        return wire

    def execute(self, statement, params=None):
        super().execute(statement, params)
        if self.description is not None:
            self.description = tuple(self.description)


def date_target(monkeypatch):
    database = DateDatabase()

    def connect(_alias):
        connection = _DateConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database
