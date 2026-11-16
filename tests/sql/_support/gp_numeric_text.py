"""Numeric assignment over unchanged psycopg2 adaptation and VALUES rendering."""

from __future__ import annotations

import math
import struct
from copy import deepcopy
from decimal import Decimal, localcontext

from sqlglot import exp

from tests.sql._support.gp_dates import DateDatabase, _DateConnection
from tests.sql._support.load_table import load_df_module


def _literal_text(node):
    while isinstance(node, exp.Cast):
        node = node.this
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Literal):
        return node.this
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return "-" + node.this.this
    message = "Native numeric assignment received an unsupported SQL expression."
    raise ValueError(message)


def _shape(value):
    digits, exponent = len(value.as_tuple().digits), value.as_tuple().exponent
    return max(0, digits + exponent), max(0, -exponent)


def _numeric_input_origin(node):
    numeric_cast = False
    while isinstance(node, exp.Cast):
        requested = node.args["to"].sql(dialect="postgres").upper()
        numeric_cast |= requested.startswith(("NUMERIC", "DECIMAL"))
        node = node.this
    if isinstance(node, exp.Neg):
        node = node.this
    return numeric_cast or not (isinstance(node, exp.Literal) and node.is_string)


class NumericDatabase(DateDatabase):
    def __init__(self, native_type, *, exists):
        super().__init__()
        self.target.schema = {"payload": native_type}
        seed = Decimal("101.25") if native_type.startswith("NUMERIC") else 101.25
        self.target.rows = [{"payload": seed}]
        self.old_rows = deepcopy(self.target.rows) if exists else []
        if not exists:
            self.tables.clear()

    @property
    def schema(self):
        return self.target.schema if ("sandbox", "events") in self.tables else {}

    @property
    def rows(self):
        return self.target.rows if ("sandbox", "events") in self.tables else []

    @staticmethod
    def decode_value(node, native_type):
        text = _literal_text(node)
        if text is None:
            return None
        native = native_type.upper().replace(" ", "")
        if native == "TEXT":
            assert isinstance(node, exp.Literal)
            assert node.is_string
            return text
        value = Decimal(text)
        if not value.is_finite():
            message = "Native numeric assignment requires a finite value."
            raise ValueError(message)
        if native in {"REAL", "FLOAT4", "DOUBLEPRECISION", "FLOAT8"}:
            # PostgreSQL parses untyped -0.0 as NUMERIC, whose zero is positive.
            # Quoted float typinput ('-0.0'::REAL/FLOAT8) preserves its IEEE sign.
            if value.is_zero() and _numeric_input_origin(node):
                value = Decimal(0)
            converted = float(value)
            if not math.isfinite(converted):
                message = "Native floating assignment exceeds its finite range."
                raise ValueError(message)
            return (
                struct.unpack("<f", struct.pack("<f", converted))[0]
                if native in {"REAL", "FLOAT4"}
                else converted
            )
        if native.startswith(("NUMERIC(", "DECIMAL(")):
            precision, scale = map(int, native.partition("(")[2].rstrip(")").split(","))
            with localcontext() as context:
                context.prec = max(precision + scale + 2, len(value.as_tuple().digits) + scale + 2)
                value = value.quantize(Decimal(1).scaleb(-scale))
            if _shape(value)[0] > precision - scale:
                message = "Native numeric assignment exceeds its integral typmod."
                raise ValueError(message)
            return value
        assert native in {"NUMERIC", "DECIMAL"}
        integral, fractional = _shape(value)
        if integral > 131072 or fractional > 16383:
            message = "Native unparameterized numeric assignment exceeds its storage domain."
            raise ValueError(message)
        return value


class _NumericConnection(_DateConnection):
    def __init__(self, database):
        super().__init__(database)
        self.closed = False

    def close(self):
        super().close()
        self.closed = True


def numeric_text_target(monkeypatch, native_type, *, exists=True):
    database = NumericDatabase(native_type, exists=exists)

    def connect(_alias):
        connection = _NumericConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database
