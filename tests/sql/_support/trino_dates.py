"""Public DATE writes over actual Trino parameter formatting and catalog metadata."""

from __future__ import annotations

import datetime as dt

from sqlglot import exp, parse_one

from tests.sql._support.load_table import load_df_module
from tests.sql._support.trino_time_values import (
    NativeTimeConnection,
    NativeTimeCursor,
    NativeTimeDatabase,
)


class _DateDatabase(NativeTimeDatabase):
    def __init__(self):
        super().__init__("DATE")
        self.table_rows[self.target] = [dt.date(1999, 12, 31)]

    def decode_cast(self, wire):
        """Apply the DATE cast to the selected SDK's actual literal expression."""
        value = parse_one(wire, read="trino")
        while isinstance(value, exp.Cast):
            value = value.this
        if isinstance(value, exp.Null):
            return None
        assert isinstance(value, exp.Literal)
        assert value.is_string
        return dt.date.fromisoformat(value.this)


class _DateConnection(NativeTimeConnection):
    def cursor(self):
        return _DateCursor(self)


class _DateCursor(NativeTimeCursor):
    def _select(self, tree, values):
        table = tree.find(exp.Table)
        if table is None or table.db != "information_schema" or table.name != "columns":
            return super()._select(tree, values)
        database = self.connection.database
        identity = table.catalog, values[0], values[1]
        if identity not in database.table_types:
            return None
        record = {
            "table_catalog": identity[0],
            "table_schema": identity[1],
            "table_name": identity[2],
            "column_name": "payload",
            "data_type": database.table_types[identity],
            "is_nullable": "YES",
            "column_default": None,
            "ordinal_position": 1,
        }
        if any(
            comparison.expression.this != record["is_nullable"]
            for comparison in tree.find_all(exp.EQ)
            if isinstance(comparison.this, exp.Column)
            and comparison.this.name == "is_nullable"
            and isinstance(comparison.expression, exp.Literal)
        ):
            return None
        self.result_rows = [tuple(record[column.name] for column in tree.expressions)]
        return None


def native_date_target(monkeypatch):
    """Replace connection acquisition; retain real adapter metadata and write gates."""
    database = _DateDatabase()

    def connect(_alias):
        connection = _DateConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database
