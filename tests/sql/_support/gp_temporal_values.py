"""Native psycopg time/interval adaptation backed by a stateful target."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy
from decimal import Decimal

from analytics_toolkit.sql.backends.type_resolution import parse_native_type
from analytics_toolkit.sql.dml.transfer.runtime import retry as transfer_retry
from sqlglot import exp, parse_one

from tests.sql._support.load_encoded_values import _EncodedCursor
from tests.sql._support.load_table import load_df_module
from tests.sql._support.transfer_not_null import (
    TransferConnection,
    TransferCursor,
    TransferDatabase,
)


class TemporalDatabase(TransferDatabase):
    def __init__(self, native_type):
        super().__init__(
            "gp", columns=("payload",), not_null=(), target_types={"payload": native_type}
        )
        self.target.rows = [
            {"payload": dt.timedelta(days=1) if "INTERVAL" in native_type else dt.time(1)},
            {"payload": dt.timedelta(days=2) if "INTERVAL" in native_type else dt.time(2)},
        ]
        if "WITH TIME ZONE" in native_type or native_type == "TIMETZ":
            self.target.rows = [
                {"payload": dt.time(hour, tzinfo=dt.timezone.utc)} for hour in (1, 2)
            ]
        self.bound_rows = []
        self.wire_rows = []
        self.lost_insert_acknowledgements = 0
        self.existence_probes = []

    def metadata(self, statement, params):
        rows = super().metadata(statement, params)
        if "to_regclass" in statement:
            self.existence_probes.append((len(self.commands) - 1, params[0], bool(rows[0][0])))
        return rows

    def _insert(self, tree, identity, params):
        assert params is None
        assert isinstance(tree.expression, exp.Values)
        names = [column.name for column in tree.this.expressions]
        schema = self.tables[identity].schema
        expressions = [row.expressions for row in tree.expression.expressions]
        rows = [
            tuple(self.decode_value(value, schema[name]) for value, name in zip(values, names))
            for values in expressions
        ]
        candidate = deepcopy(self.tables[identity])
        candidate.insert(names, rows)
        self.tables[identity] = candidate
        self.wire_rows.extend(
            tuple(value.sql(dialect="postgres") for value in row) for row in expressions
        )

    @staticmethod
    def decode_value(node, native_type):
        while isinstance(node, exp.Cast):
            node = node.this
        if isinstance(node, exp.Null):
            return None
        if isinstance(node, exp.Interval):
            node = node.this
        assert isinstance(node, exp.Literal)
        assert node.is_string
        if "INTERVAL" in native_type:
            days, day_unit, seconds, second_unit = node.this.split()
            assert day_unit == "days"
            assert second_unit == "seconds"
            value = dt.timedelta(days=int(days), microseconds=int(Decimal(seconds) * 1_000_000))
        elif native_type.startswith("TEXT"):
            return node.this
        else:
            value = dt.time.fromisoformat(node.this)
        parsed = parse_native_type(native_type)
        precision = int(parsed.arguments[0]) if parsed.arguments else 6
        factor = 10 ** (6 - precision)
        # Emulate the native target typmod on the actual adapted wire value.
        # Facade rounding cases stay away from day/end-of-day carry boundaries.
        micros = value.microseconds if isinstance(value, dt.timedelta) else value.microsecond
        rounded = (micros + factor // 2) // factor * factor
        return (
            value + dt.timedelta(microseconds=rounded - micros)
            if isinstance(value, dt.timedelta)
            else value.replace(microsecond=rounded)
        )


class TemporalConnection(TransferConnection):
    encoding = "UTF8"

    @property
    def bound_rows(self):
        return self.database.bound_rows

    def cursor(self):
        return TemporalCursor(self)

    def commit(self):
        super().commit()
        if getattr(self, "insert_pending", False):
            self.insert_pending = False
            if self.database.lost_insert_acknowledgements:
                self.database.lost_insert_acknowledgements -= 1
                message = "Lost acknowledgement after committing native temporal insert."
                raise ConnectionError(message)


class TemporalCursor(TransferCursor):
    mogrify = _EncodedCursor.mogrify

    def execute(self, statement, params=None):
        text = statement.decode("utf-8") if isinstance(statement, bytes) else statement
        super().execute(text, params)
        database = self.connection.database
        parsed = database._ddl_schema_prefix(text)
        self.connection.insert_pending = isinstance(
            parse_one(parsed, read=database.dialect), exp.Insert
        )


def temporal_target(monkeypatch, native_type, *, exists=True):
    database = TemporalDatabase(native_type)
    if not exists:
        database.tables.clear()
    monkeypatch.setattr(
        load_df_module, "get_sql_connection", lambda _key: TemporalConnection(database)
    )
    monkeypatch.setattr(
        transfer_retry, "get_sql_connection", lambda _key: TemporalConnection(database)
    )
    return database
