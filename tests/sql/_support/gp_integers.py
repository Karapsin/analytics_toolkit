"""Integer target assignment over unchanged psycopg2 VALUES formatting."""

from __future__ import annotations

import re
from copy import deepcopy

from sqlglot import exp

from tests.sql._support.gp_dates import DateDatabase, _DateConnection
from tests.sql._support.load_table import load_df_module

_DECIMAL_INTEGER = re.compile(r"[+-]?[0-9]+", flags=re.ASCII)
_ASCII_WHITESPACE = " \t\r\n\v\f"
_CANONICAL = {
    "SMALLINT": "SMALLINT",
    "INT2": "SMALLINT",
    "INTEGER": "INTEGER",
    "INT": "INTEGER",
    "INT4": "INTEGER",
    "BIGINT": "BIGINT",
    "INT8": "BIGINT",
}
_WIDTHS = {"SMALLINT": 16, "INTEGER": 32, "BIGINT": 64}


class IntegerDatabase(DateDatabase):
    def __init__(self, native_type, *, exists):
        super().__init__()
        canonical = _CANONICAL[native_type.upper()]
        self.target.schema = {"payload": canonical}
        self.target.rows = [{"payload": 101}, {"payload": 201}]
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
        while isinstance(node, exp.Cast):
            node = node.this
        if isinstance(node, exp.Null):
            return None
        native = native_type.upper()
        if native == "TEXT":
            assert isinstance(node, exp.Literal)
            assert node.is_string
            return node.this
        text = _integer_expression(node)
        if _DECIMAL_INTEGER.fullmatch(text) is None:
            message = "Server rejected nondecimal integer input."
            raise ValueError(message)
        value = int(text)
        width = _WIDTHS[_CANONICAL[native]]
        if not -(2 ** (width - 1)) <= value <= 2 ** (width - 1) - 1:
            message = "Server rejected integer input outside the native target width."
            raise ValueError(message)
        return value


def _integer_expression(node):
    if isinstance(node, exp.Literal):
        return node.this.strip(_ASCII_WHITESPACE) if node.is_string else node.this
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        assert not node.this.is_string
        return "-" + node.this.this
    message = "Server rejected unsupported integer input expression."
    raise ValueError(message)


def integer_target(monkeypatch, native_type, exists=True):
    database = IntegerDatabase(native_type, exists=exists)

    def connect(_alias):
        connection = _DateConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database
