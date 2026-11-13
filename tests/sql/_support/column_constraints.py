"""DBAPI metadata boundary for the actual required-column adapter hooks."""

from __future__ import annotations

from types import SimpleNamespace


def metadata_connection(rows, *, failure=None):
    statements = []
    closed = []

    def execute(statement, params):
        statements.append((statement, params))
        if failure is not None:
            raise failure

    cursor = SimpleNamespace(
        execute=execute,
        fetchall=lambda: rows,
        close=lambda: closed.append(True),
    )
    return SimpleNamespace(cursor=lambda: cursor), statements, closed
