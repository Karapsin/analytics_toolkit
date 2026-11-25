"""Evaluate native clock GROUP BY and null-safe overlap at a real SDK boundary."""

from __future__ import annotations

from collections import Counter

from sqlglot import exp

from tests.sql._support.load_table import load_df_module
from tests.sql._support.trino_time_values import (
    NativeTimeConnection,
    NativeTimeCursor,
    native_time_target,
)


class _ClockKeyCursor(NativeTimeCursor):
    def _select(self, tree, values):
        database = self.connection.database
        if tree.args.get("group") is not None:
            source = tree.args["from_"].this
            group = tree.args["group"].expressions
            assert len(group) == 1
            assert isinstance(group[0], exp.Column)
            assert group[0].name == "payload"
            assert tree.args.get("having") is not None
            counts = Counter(database.table_rows[database.table_key(source)])
            self.result_rows = [(1,)] if any(count > 1 for count in counts.values()) else []
        elif tree.args.get("joins"):
            tables = [tree.args["from_"].this]
            tables.extend(join.this for join in tree.args["joins"])
            assert len(tables) == 2
            rows = [database.table_rows[database.table_key(table)] for table in tables]
            aliases = [table.alias_or_name for table in tables]
            predicate = tree.args["joins"][0].args["on"]
            columns = list(predicate.find_all(exp.Column))
            assert columns
            assert all(column.name == "payload" for column in columns)
            assert {column.table for column in columns} == set(aliases)
            self.result_rows = (
                [(1,)]
                if any(
                    _predicate(predicate, dict(zip(aliases, (left, right))))
                    for left in rows[0]
                    for right in rows[1]
                )
                else []
            )
        else:
            super()._select(tree, values)


def _predicate(node, row):
    if isinstance(node, exp.Paren):
        return _predicate(node.this, row)
    if isinstance(node, exp.Column):
        return row[node.table]
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.And):
        return _predicate(node.left, row) and _predicate(node.right, row)
    if isinstance(node, exp.Or):
        return _predicate(node.left, row) or _predicate(node.right, row)
    assert isinstance(node, (exp.EQ, exp.Is, exp.NullSafeEQ))
    return _predicate(node.left, row) == _predicate(node.right, row)


class _ClockKeyConnection(NativeTimeConnection):
    def cursor(self):
        return _ClockKeyCursor(self)


def clock_key_target(monkeypatch):
    database = native_time_target(monkeypatch, "TIME(6)")

    def connect(_alias):
        connection = _ClockKeyConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database
