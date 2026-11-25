"""Fixed native timestamp-coordinate cases over actual Trino SDK literals."""

from __future__ import annotations

import re

from sqlglot import exp, parse_one

from tests.sql._support.load_table import load_df_module
from tests.sql._support.trino_dates import _DateCursor
from tests.sql._support.trino_time_values import NativeTimeConnection, NativeTimeDatabase


class TimestampBoundsDatabase(NativeTimeDatabase):
    """Model only verified far-outside and representable control years."""

    def __init__(self, native, *, connector="memory", format_version=3):
        super().__init__(native)
        self.connector = connector
        self.format_version = format_version
        self.table_rows[self.target] = ["preserved-original"]

    def decode_cast(self, wire):
        tree = parse_one(wire, read="trino")
        while isinstance(tree, exp.Cast):
            tree = tree.this
        if isinstance(tree, exp.Null):
            return None
        assert isinstance(tree, exp.Literal)
        assert tree.is_string
        year = int(re.match(r"([+-]?[0-9]+)-", tree.this)[1])
        # The fixed matrix deliberately stays far from all endpoints: 1M is
        # outside signed64 epoch micros; 100k is outside packed52 epoch millis.
        outside = abs(year) >= (100000 if "WITH TIME ZONE" in self.native_type else 1000000)
        outside |= self.connector == "iceberg" and "(9)" in self.native_type and year == 10000
        if outside:
            message = "Native timestamp coordinate exceeds its storage representation."
            raise ValueError(message)
        assert year in {0, 10000}
        return tree.this


class _Connection(NativeTimeConnection):
    def cursor(self):
        return _Cursor(self)


class _Cursor(_DateCursor):
    def execute(self, query, params=None):
        if "system.metadata.catalogs" in query:
            self.result_rows = [(self.connection.database.connector,)]
            self.connection.database.statements.append((query, list(params or []), []))
            return None
        if "SHOW CREATE TABLE" in query:
            database = self.connection.database
            self.result_rows = [
                (
                    f"CREATE TABLE iceberg.sandbox.target (payload {database.native_type}) "
                    f"WITH (format_version = {database.format_version}, format = 'PARQUET')",
                )
            ]
            database.statements.append((query, list(params or []), []))
            return None
        return super().execute(query, params)


def timestamp_bounds_target(monkeypatch, native, **options):
    database = TimestampBoundsDatabase(native, **options)

    def connect(_alias):
        connection = _Connection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database
