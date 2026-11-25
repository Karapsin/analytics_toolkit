"""Bounded native timestamp assignment over real psycopg2 VALUES formatting."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

from psycopg2 import DataError
from sqlglot import exp, parse_one

from tests.sql._support.gp_dates import DateDatabase, _DateConnection
from tests.sql._support.gp_relation_identity import RelationIdentityDatabase
from tests.sql._support.load_table import load_df_module


class _TimestampInputError(DataError):
    @property
    def pgcode(self):
        return "22007"


class TimestampDatabase(RelationIdentityDatabase, DateDatabase):
    """Model only the tested ISO input subset, not PostgreSQL's input grammar."""

    def __init__(self, native_type):
        super().__init__()
        self.target.schema = {"payload": native_type}
        self.target.rows = [{"payload": dt.datetime.fromisoformat("2001-01-02T03:04:05.000000")}]
        self.old_rows = deepcopy(self.target.rows)
        self.settings = {
            "timezone": "Europe/Berlin",
            "datestyle": "ISO, MDY",
            "timezone_abbreviations": "Default",
            "intervalstyle": "postgres",
        }

    def metadata(self, statement, params):
        if "current_setting" in statement.lower():
            tree = parse_one(statement, read="postgres")
            settings = list(tree.find_all(exp.Anonymous)) if tree is not None else []
            requested = [
                node.expressions[0].this.lower()
                for node in settings
                if node.name.lower() == "current_setting"
            ]
            assert requested
            return [tuple(self.settings[name] for name in requested)]
        return super().metadata(statement, params)

    @staticmethod
    def assign(value, native_type):
        if value is None:
            return None
        native = native_type.upper()
        if native in {"TEXT", "VARCHAR"}:
            assert isinstance(value, str)
            return value
        assert native.startswith("TIMESTAMP")
        assert "WITH TIME ZONE" not in native
        try:
            assigned = dt.datetime.fromisoformat(value) if isinstance(value, str) else value
        except ValueError as error:
            message = "Native timestamp input rejected."
            raise _TimestampInputError(message) from error
        assert isinstance(assigned, dt.datetime)
        assert assigned.tzinfo is None
        precision = int(native.partition("(")[2].partition(")")[0]) if "(" in native else 6
        unit = 10 ** (6 - precision)
        epoch = dt.datetime.fromisoformat("2000-01-01T00:00:00.000000")
        elapsed = assigned - epoch
        micros = (elapsed.days * 86400 + elapsed.seconds) * 10**6 + elapsed.microseconds
        rounded = (1 if micros >= 0 else -1) * ((abs(micros) + unit // 2) // unit) * unit
        return epoch + dt.timedelta(microseconds=rounded)

    @classmethod
    def decode_value(cls, node, native_type):
        while isinstance(node, exp.Cast):
            node = node.this
        if isinstance(node, exp.Null):
            return None
        assert isinstance(node, exp.Literal)
        assert node.is_string
        return cls.assign(node.this, native_type)

    def value(self, node, row):
        if isinstance(node, exp.Cast):
            native = node.args["to"].sql(dialect="postgres")
            return self.assign(self.value(node.this, row), native)
        return super().value(node, row)

    def matches(self, condition, row):
        if isinstance(condition, exp.NullSafeNEQ):
            return self.value(condition.left, row) != self.value(condition.right, row)
        return super().matches(condition, row)

    def _insert(self, tree, identity, params):
        assert params is None
        names = [column.name for column in tree.this.expressions]
        schema = self.tables[identity].schema
        if isinstance(tree.expression, exp.Values):
            expressions = [row.expressions for row in tree.expression.expressions]
            rows = [
                tuple(self.decode_value(value, schema[name]) for value, name in zip(row, names))
                for row in expressions
            ]
            self.wire_rows.extend(
                tuple(value.sql(dialect="postgres") for value in row) for row in expressions
            )
        else:
            assert isinstance(tree.expression, exp.Select)
            selected, _columns = self.select(tree.expression)
            rows = [
                tuple(self.assign(value, schema[name]) for value, name in zip(row, names))
                for row in selected
            ]
        candidate = deepcopy(self.tables[identity])
        candidate.insert(names, rows)
        self.tables[identity] = candidate
        if identity != ("sandbox", "events"):
            self.stage_inserts.append((identity, deepcopy(rows)))


def timestamp_target(monkeypatch, native_type):
    database = TimestampDatabase(native_type)

    def connect(_alias):
        connection = _DateConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database
