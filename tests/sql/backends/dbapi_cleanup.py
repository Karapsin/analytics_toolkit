"""Pure DBAPI cursor cleanup cannot change committed or failed SQL outcomes."""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter

from tests.sql._support.execute_read import FakeDbapiConnection

api = importlib.import_module("analytics_toolkit.sql.ddl.api")
execute_read = importlib.import_module("analytics_toolkit.sql.dml.io.execute_read")


class Cursor:
    rowcount = 42

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.close_calls = 0

    def execute(self, query: str) -> None:
        self.connection.queries.append(query)
        if self.connection.failure_phase == "execute":
            raise self.connection.failure

    def close(self) -> None:
        self.close_calls += 1
        message = "cursor cleanup failed after known SQL outcome"
        raise OSError(message)


class Connection:
    def __init__(self, failure_phase: str | None = None) -> None:
        self.failure_phase = failure_phase
        self.failure = RuntimeError("primary SQL or commit failure")
        self.queries: list[str] = []
        self.commits = self.rollbacks = self.closed = 0
        self.cursors: list[Cursor] = []

    def cursor(self) -> Cursor:
        cursor = Cursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commits += 1
        if self.failure_phase == "commit":
            raise self.failure

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


@pytest.mark.parametrize("failure_phase", [None, "execute", "commit"])
def test_public_create_committed_commands_are_not_replayed_for_cursor_close(
    monkeypatch: pytest.MonkeyPatch, failure_phase: str | None
) -> None:
    connection = Connection(failure_phase)
    opened: list[str] = []

    def opener(key: str) -> Connection:
        opened.append(key)
        return connection

    monkeypatch.setattr(api, "get_sql_connection", opener)
    if failure_phase:
        with pytest.raises(RuntimeError, match="primary SQL or commit failure") as caught:
            sql.create_table("gp", "sandbox.target", table_schema={"id": "BIGINT"}, retry_cnt=1)
        assert caught.value is connection.failure
        assert connection.rollbacks >= 1
    else:
        sql.create_table(
            "gp",
            "sandbox.target",
            table_schema={"id": "BIGINT"},
            retry_cnt=3,
            timeout_increment=0,
        )
        assert connection.commits == 1
        assert connection.rollbacks == 0
    assert opened == ["gp"]
    assert connection.closed == 1
    assert len(connection.queries) == 1
    assert connection.cursors[0].close_calls == 1


@pytest.mark.parametrize("method", ["execute_command", "execute_commands", "insert_from_query"])
@pytest.mark.parametrize("failure_phase", [None, "execute", "commit"])
def test_dbapi_write_boundaries_preserve_commit_and_primary_failure(
    method: str, failure_phase: str | None
) -> None:
    connection = Connection(failure_phase)
    adapter = get_backend_adapter("gp")

    def operation() -> Any:
        if method == "insert_from_query":
            return adapter.insert_from_query(
                connection, "sandbox.target", "SELECT 42 AS id", {"id": "BIGINT"}
            )
        query: Any = (
            ["CREATE TABLE sandbox.target (id BIGINT)"]
            if method == "execute_commands"
            else "INSERT INTO sandbox.target SELECT 42"
        )
        return getattr(adapter, method)(connection, query)

    if failure_phase:
        with pytest.raises(RuntimeError, match="primary SQL or commit failure") as caught:
            operation()
        assert caught.value is connection.failure
        assert connection.rollbacks == 1
    else:
        result = operation()
        if method == "insert_from_query":
            assert result == 42
        assert connection.commits == 1
        assert connection.rollbacks == 0
    assert connection.cursors[0].close_calls == 1


@pytest.mark.parametrize("backend", ["gp", "trino"])
@pytest.mark.parametrize("fails", [False, True])
def test_execute_read_setup_cursor_close_never_replays_or_masks_query(
    monkeypatch: pytest.MonkeyPatch, backend: str, fails: bool
) -> None:
    connection = FakeDbapiConnection()
    opened: list[str] = []
    failure = RuntimeError("primary final read failure")

    def opener(key: str) -> Any:
        opened.append(key)
        return connection

    def close_cursor() -> None:
        message = "close failure after query outcome"
        raise OSError(message)

    def fail_query(_query: str) -> None:
        raise failure

    monkeypatch.setattr(execute_read, "get_sql_connection", opener)
    monkeypatch.setattr(connection.cursor_obj, "close", close_cursor)
    if fails:
        monkeypatch.setattr(connection.cursor_obj, "execute", fail_query)
        with pytest.raises(RuntimeError, match="primary final read failure") as caught:
            sql.execute_read(backend, "CREATE TEMP TABLE temp (id INT); SELECT 1", retry_cnt=1)
        assert caught.value is failure
    else:
        result = sql.execute_read(
            backend,
            "CREATE TEMP TABLE temp (id INT); SELECT 1",
            retry_cnt=3,
            timeout_increment=0,
        )
        assert len(result) == 1
        assert len(connection.executed) == 2
    assert opened == [backend]
    assert connection.close_calls == 1


def test_dbapi_post_write_count_close_does_not_fail_validation() -> None:
    connection = Connection()
    cursor = Cursor(connection)
    cursor.fetchone = lambda: (42,)
    connection.cursor = lambda: cursor
    assert get_backend_adapter("gp").count_table_rows(connection, "sandbox.target") == 42
    assert cursor.close_calls == 1
