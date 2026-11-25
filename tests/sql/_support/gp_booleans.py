"""BOOLEAN assignment over the real psycopg2 VALUES and scalar adapters."""

from __future__ import annotations

from copy import deepcopy

from sqlglot import exp

from tests.sql._support.gp_dates import DateDatabase, _DateConnection
from tests.sql._support.load_table import load_df_module

_BOOLEAN_TEXT = {
    "t": True,
    "tr": True,
    "tru": True,
    "true": True,
    "y": True,
    "ye": True,
    "yes": True,
    "on": True,
    "1": True,
    "f": False,
    "fa": False,
    "fal": False,
    "fals": False,
    "false": False,
    "n": False,
    "no": False,
    "of": False,
    "off": False,
    "0": False,
}


class BooleanDatabase(DateDatabase):
    def __init__(self):
        super().__init__()
        self.target.schema = {"payload": "BOOLEAN"}
        self.target.rows = [{"payload": False}, {"payload": True}]
        self.old_rows = deepcopy(self.target.rows)

    @staticmethod
    def decode_value(node, native_type):
        assert native_type.upper() in {"BOOLEAN", "BOOL"}
        while isinstance(node, exp.Cast):
            node = node.this
        if isinstance(node, exp.Null):
            return None
        if isinstance(node, exp.Boolean):
            assert isinstance(node.this, bool)
            return node.this
        if isinstance(node, exp.Literal) and node.is_string:
            text = node.this.strip(" \t\r\n\v\f")
            if text.isascii() and text.lower() in _BOOLEAN_TEXT:
                return _BOOLEAN_TEXT[text.lower()]
        message = "Server rejected unknown BOOLEAN input text."
        raise ValueError(message)


def boolean_target(monkeypatch):
    database = BooleanDatabase()

    def connect(_alias):
        connection = _DateConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database
