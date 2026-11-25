from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tests.sql._support.retries import execute_read_module, execute_sql_module, read_sql_module

if TYPE_CHECKING:
    import pytest


class TerminalCursorError(Exception):
    def __init__(self, backend: str) -> None:
        super().__init__("missing target relation")
        if backend == "gp":
            self.pgcode = "42P01"
        else:
            self.error_name = "TABLE_NOT_FOUND"


class RecordingCursor:
    description = (("value",),)
    rowcount = 1

    def __init__(self, connection: RecordingConnection) -> None:
        self.connection = connection
        self.close_calls = 0
        self.enter_calls = 0
        self.exit_calls = 0
        self.exit_error: BaseException | None = None

    def __enter__(self):
        self.enter_calls += 1
        if self.connection.enter_error is not None:
            raise self.connection.enter_error
        return self

    def __exit__(self, _kind: object, error: BaseException | None, _traceback: object) -> None:
        self.exit_calls += 1
        self.exit_error = error
        self.close()

    def execute(self, statement: str, parameters: Any = None) -> None:
        self.connection.executed.append((statement, parameters))
        if self.connection.execute_error is not None:
            raise self.connection.execute_error

    def fetchall(self) -> list[tuple[int]]:
        return [(1,)]

    def close(self) -> None:
        self.close_calls += 1
        if self.connection.close_error is not None:
            raise self.connection.close_error


class RecordingConnection:
    def __init__(
        self,
        *,
        close_error: Exception | None = None,
        execute_error: Exception | None = None,
        commit_error: Exception | None = None,
        cursor_error: Exception | None = None,
        enter_error: Exception | None = None,
    ) -> None:
        self.close_error = close_error
        self.execute_error = execute_error
        self.commit_error = commit_error
        self.cursor_error = cursor_error
        self.enter_error = enter_error
        self.executed: list[tuple[str, Any]] = []
        self.cursors: list[RecordingCursor] = []
        self.cursor_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0
        self.bulk_rows: list[tuple[Any, ...]] = []

    def cursor(self) -> RecordingCursor:
        self.cursor_calls += 1
        if self.cursor_error is not None:
            raise self.cursor_error
        cursor = RecordingCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commit_calls += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1


def arrange_connection(
    monkeypatch: pytest.MonkeyPatch, connection: RecordingConnection
) -> list[str]:
    opens: list[str] = []

    def open_connection(key: str) -> RecordingConnection:
        opens.append(key)
        return connection

    for module in (execute_sql_module, execute_read_module, read_sql_module):
        monkeypatch.setattr(module, "get_sql_connection", open_connection)
    return opens


def record_bulk_values(
    cursor: RecordingCursor,
    statement: str,
    rows: Any,
    *,
    page_size: int,
) -> None:
    del page_size
    cursor.connection.executed.append((statement, None))
    cursor.connection.bulk_rows.extend(tuple(row) for row in rows)
    if cursor.connection.execute_error is not None:
        raise cursor.connection.execute_error
