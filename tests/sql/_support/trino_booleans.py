"""Real Trino parameter formatting with native integer and string BOOLEAN casts."""

from __future__ import annotations

from decimal import Decimal

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
        decimal = isinstance(value, exp.Cast) and value.args["to"].is_type("DECIMAL")
        while isinstance(value, exp.Cast):
            value = value.this
        if isinstance(value, exp.Null):
            return None
        if isinstance(value, exp.Boolean):
            return value.this
        if decimal:
            assert isinstance(value, exp.Literal)
            assert value.is_string
            return Decimal(value.this) != 0
        sign = 1
        if isinstance(value, exp.Neg):
            sign = -1
            value = value.this
        assert isinstance(value, exp.Literal)
        if value.is_string:
            aliases = {"true": True, "false": False, "t": True, "f": False, "1": True, "0": False}
            text = value.this
            if text.isascii() and text.casefold() in aliases:
                return aliases[text.casefold()]
            message = "Trino rejects unsupported BOOLEAN string casts."
            raise ValueError(message)
        assert not value.is_string
        number = value.to_py()
        assert type(number) in {int, float}
        # Trino's BIGINT/DOUBLE/DECIMAL operators use nonzero numeric equality.
        return sign * number != 0


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
