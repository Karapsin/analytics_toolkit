"""Real Trino parameter formatting with native BIGINT-to-BOOLEAN cast semantics."""

from __future__ import annotations

from sqlglot import exp, parse_one

from tests.sql._support.load_table import load_df_module
from tests.sql._support.trino_dates import _DateCursor
from tests.sql._support.trino_time_values import NativeTimeConnection, NativeTimeDatabase


class _BooleanDatabase(NativeTimeDatabase):
    def __init__(self):
        super().__init__("BOOLEAN")
        self.table_rows[self.target] = [True]

    def decode_cast(self, wire):
        """Apply BOOLEAN to the actual SDK literal, without Python string truthiness."""
        value = parse_one(wire, read="trino")
        while isinstance(value, exp.Cast):
            value = value.this
        if isinstance(value, exp.Null):
            return None
        if isinstance(value, exp.Boolean):
            return value.this
        assert isinstance(value, exp.Literal)
        assert not value.is_string
        integer = value.to_py()
        assert type(integer) is int
        # Primary source: trinodb/trino BigintOperators.castToBoolean returns value != 0.
        return integer != 0


class _BooleanConnection(NativeTimeConnection):
    def cursor(self):
        # This cursor's catalog projection is generic; DATE casts live in its database.
        return _DateCursor(self)


def native_boolean_target(monkeypatch):
    """Replace only connection acquisition; retain catalog, gates, writer and SDK."""
    database = _BooleanDatabase()

    def connect(_alias):
        connection = _BooleanConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database
