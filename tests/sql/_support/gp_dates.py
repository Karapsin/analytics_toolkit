"""Native DATE assignment over real psycopg2 adaptation and VALUES formatting."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

from sqlglot import exp, parse_one

from tests.sql._support.gp_relation_identity import RelationIdentityDatabase
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


class _NativeDateDatabase(RelationIdentityDatabase, DateDatabase):
    """Owned DATE checkpoints use this fixture's existing UTC server session."""

    def __init__(self):
        super().__init__()
        self.target.native_creation_oid = 10000
        self._next_relation_oid = 10000
        self.settings = {
            "timezone": "UTC",
            "datestyle": "ISO, MDY",
            "timezone_abbreviations": "Default",
            "intervalstyle": "postgres",
        }

    def metadata(self, statement, params):
        if "current_setting" in statement.lower():
            tree = parse_one(statement, read="postgres")
            names = [
                node.expressions[0].this.lower()
                for node in tree.find_all(exp.Anonymous)
                if node.name.lower() == "current_setting"
            ]
            return [tuple(self.settings[name] for name in names)]
        return super().metadata(statement, params)

    def _insert(self, tree, identity, params):
        if isinstance(tree.expression, exp.Values):
            return super()._insert(tree, identity, params)
        assert params is None
        names = [column.name for column in tree.this.expressions]
        rows, _columns = self.select(tree.expression)
        assert all(value is None or type(value) is dt.date for row in rows for value in row)
        candidate = deepcopy(self.tables[identity])
        candidate.insert(names, rows)
        self.tables[identity] = candidate
        return None


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
    database = _NativeDateDatabase()

    def connect(_alias):
        connection = _DateConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database
