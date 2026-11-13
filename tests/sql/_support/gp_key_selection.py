"""Evaluate emitted PostgreSQL timestamp key predicates at a fake DB-API boundary."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlglot import exp, parse_one


class TimestampKeyCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = [
            ("stamp", 1184 if connection.aware_column else 1114, None, None, None, None, None),
            ("row_id", 25, None, None, None, None, None),
        ]
        self.rows = []
        self.closed = False

    def execute(self, statement):
        self.connection.queries.append(statement)
        predicate = parse_one(statement, read="postgres").find(exp.EQ)
        assert predicate is not None
        cast = predicate.right
        assert isinstance(cast, exp.Cast)
        assert isinstance(cast.this, exp.Literal)
        assert cast.this.is_string
        value = datetime.fromisoformat(cast.this.this)
        if cast.to.this == exp.DataType.Type.TIMESTAMP:
            # PostgreSQL ignores an offset inside a timestamp-without-zone literal.
            value = value.replace(tzinfo=None)
        else:
            assert cast.to.this == exp.DataType.Type.TIMESTAMPTZ
        if self.connection.aware_column and value.tzinfo is None:
            value = value.replace(tzinfo=self.connection.session_timezone)
        if not self.connection.aware_column and value.tzinfo is not None:
            self.rows = [
                row
                for row in self.connection.rows
                if row[0].replace(tzinfo=self.connection.session_timezone) == value
            ]
        else:
            self.rows = [row for row in self.connection.rows if row[0] == value]

    def fetchmany(self, size):
        result = self.rows[:size]
        self.rows = self.rows[size:]
        return result

    def close(self):
        self.closed = True


class TimestampKeyConnection:
    session_timezone = timezone.utc

    def __init__(self, *, aware_column):
        self.aware_column = aware_column
        zone = timezone.utc if aware_column else None
        self.rows = [
            (datetime(2025, 1, 1, 0, 0, 0, 123456, tzinfo=zone), "midnight"),
            (datetime(2025, 1, 1, 3, 0, 0, 123456, tzinfo=zone), "three"),
        ]
        self.queries = []
        self.cursors = []
        self.closed = False

    def cursor(self):
        cursor = TimestampKeyCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True
