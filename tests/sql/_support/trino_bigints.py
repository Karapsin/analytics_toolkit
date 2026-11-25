"""Real Trino BIGINT parameters and catalog rows over an isolated DB-API endpoint."""

from __future__ import annotations

from sqlglot import exp, parse_one

from tests.sql._support.load_table import load_df_module
from tests.sql._support.trino_dates import _DateCursor
from tests.sql._support.trino_time_values import NativeTimeConnection, NativeTimeDatabase


class _BigintDatabase(NativeTimeDatabase):
    def __init__(self):
        super().__init__("BIGINT")
        self.table_rows[self.target] = [101, 201]

    def decode_cast(self, wire):
        """Retain exact integer literals produced by the installed SDK formatter."""
        value = parse_one(wire, read="trino")
        while isinstance(value, exp.Cast):
            value = value.this
        if isinstance(value, exp.Null):
            return None
        sign = 1
        if isinstance(value, exp.Neg):
            sign = -1
            value = value.this
        assert isinstance(value, exp.Literal)
        assert not value.is_string
        integer = value.to_py()
        assert type(integer) is int
        integer *= sign
        if not -(2**63) <= integer <= 2**63 - 1:
            message = "Trino rejects values outside the signed BIGINT range."
            raise ValueError(message)
        return integer


class _BigintConnection(NativeTimeConnection):
    def cursor(self):
        return _DateCursor(self)


def native_bigint_target(monkeypatch):
    """Replace acquisition only; retain real metadata, value gates and insertion."""
    database = _BigintDatabase()

    def connect(_alias):
        connection = _BigintConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database
