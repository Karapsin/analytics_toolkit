"""Finite native numeric casts over actual Trino SDK parameter expressions."""

from __future__ import annotations

import struct
from decimal import Decimal, localcontext

from sqlglot import exp, parse_one

from tests.sql._support.load_table import load_df_module
from tests.sql._support.trino_dates import _DateCursor
from tests.sql._support.trino_time_values import NativeTimeConnection, NativeTimeDatabase


class _NumericDatabase(NativeTimeDatabase):
    def __init__(self, native_type):
        super().__init__(native_type)
        self.table_rows[self.target] = [
            Decimal("99.123456789") if "DECIMAL" in native_type else 99.5
        ]
        self.physical_float_values = []

    def decode_cast(self, wire):
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
        if self.native_type.startswith("DECIMAL"):
            assert value.is_string
            number = Decimal(value.this)
            with localcontext() as context:
                context.prec = 100
                stored = number.quantize(Decimal("0.000000001"))
            assert stored == number
            assert stored.copy_abs() < Decimal("1E29")
            return stored
        number = sign * float(value.this)
        form = "<f" if self.native_type == "REAL" else "<d"
        physical = struct.pack(form, number)
        self.physical_float_values.append(physical)
        return struct.unpack(form, physical)[0]


class _NumericConnection(NativeTimeConnection):
    def cursor(self):
        return _DateCursor(self)


def native_numeric_target(monkeypatch, native_type):
    database = _NumericDatabase(native_type)

    def connect(_alias):
        connection = _NumericConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database
