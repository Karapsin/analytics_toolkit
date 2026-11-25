"""The installed Trino driver's parameter renderer with no HTTP connection."""

from __future__ import annotations

from typing import Any

from trino.dbapi import Cursor


class DriverParameterConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, list[Any], list[str]]] = []
        self.metadata_statements: list[tuple[str, list[Any], list[str]]] = []
        self.close_calls = 0
        self.cursor_close_calls = 0

    def cursor(self) -> DriverParameterCursor:
        return DriverParameterCursor(self)

    def close(self) -> None:
        self.close_calls += 1


class DriverParameterCursor:
    def __init__(self, connection: DriverParameterConnection) -> None:
        self.connection = connection
        self.formatter = object.__new__(Cursor)
        self.rowcount = -1

    def execute(self, statement: str, values: list[Any] | None = None) -> DriverParameterCursor:
        params = values or []
        rendered = [self.formatter._format_prepared_param(value) for value in params]
        if statement.startswith("SELECT column_name, is_nullable, column_default "):
            self.connection.metadata_statements.append((statement, params, rendered))
        else:
            self.connection.statements.append((statement, params, rendered))
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        # The parameter-binding target has ordinary nullable columns.
        return []

    def close(self) -> None:
        self.connection.cursor_close_calls += 1


def render_parameter(value: Any) -> str:
    return str(object.__new__(Cursor)._format_prepared_param(value))
