"""Native sibling integer casts over real Trino SDK parameter expressions."""

from __future__ import annotations

import re

from sqlglot import exp, parse_one

from tests.sql._support.load_table import load_df_module
from tests.sql._support.trino_dates import _DateCursor
from tests.sql._support.trino_time_values import NativeTimeConnection, NativeTimeDatabase

_WIDTHS = {"TINYINT": 8, "SMALLINT": 16, "INTEGER": 32}


def _ascii_trim(value):
    start, stop = 0, len(value)
    while start < stop and ord(value[start]) <= 0x20:
        start += 1
    while stop > start and ord(value[stop - 1]) <= 0x20:
        stop -= 1
    return value[start:stop]


class _IntegerDatabase(NativeTimeDatabase):
    def __init__(self, native_type):
        super().__init__(native_type)
        self.table_rows[self.target] = [11, 21]

    def decode_cast(self, wire):
        """Model the source-proven ASCII grammar and destination width after SDK formatting."""
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
        if value.is_string:
            text = _ascii_trim(value.this)
            if re.fullmatch(r"[+-]?[0-9]+", text, flags=re.ASCII) is None:
                message = "Trino rejects malformed integer text."
                raise ValueError(message)
            integer = int(text)
        else:
            integer = value.to_py()
            assert type(integer) is int
            integer *= sign
        width = _WIDTHS[self.native_type]
        if not -(2 ** (width - 1)) <= integer < 2 ** (width - 1):
            message = "Trino rejects integer text outside the destination range."
            raise ValueError(message)
        return integer


class _IntegerConnection(NativeTimeConnection):
    def cursor(self):
        return _DateCursor(self)


def native_integer_target(monkeypatch, native_type):
    """Replace connection acquisition only; retain catalog and assignment gates."""
    database = _IntegerDatabase(native_type)

    def connect(_alias):
        connection = _IntegerConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database
