"""Execution counts, close failures and cancellation enter through public APIs."""

from __future__ import annotations

import importlib
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.native_client import NativeClickHouseClient
from analytics_toolkit.sql.execution.cancellation import (
    SqlCancellationScope,
    activate_cancellation_scope,
    cancellation_query_comments,
)

from tests.sql._support.execute_read import FakeClickHouseClient

execute_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_sql")
read_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_read")


class CountCursor:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.rowcount = -1
        self.closed = 0

    def execute(self, query: str) -> None:
        self.connection.queries.append(query)
        self.rowcount = 99 if "CREATE" in query else self.connection.count
        if self.connection.backend == "trino":
            self.rowcount = -1  # Trino exposes the count only after consumption.
        if self.connection.failure:
            raise self.connection.failure

    def fetchall(self) -> list[Any]:
        self.rowcount = 99 if "CREATE" in self.connection.queries[-1] else self.connection.count
        return []

    def close(self) -> None:
        self.closed += 1
        if self.connection.close_error:
            message = "cursor close failed"
            raise OSError(message)


class CountConnection:
    def __init__(self, backend: str, count: int, *, close_error: bool = False) -> None:
        self.backend, self.count = backend, count
        self.close_error = close_error
        self.queries: list[str] = []
        self.closed = 0
        self.failure: Exception | None = None
        self.cursor_obj = CountCursor(self)

    def cursor(self) -> CountCursor:
        return self.cursor_obj

    def command(self, query: str) -> dict[str, Any]:
        self.queries.append(query)
        if self.failure:
            raise self.failure
        return {} if self.count < 0 else {"written_rows": self.count}

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self.closed += 1
        if self.close_error:
            message = "connection close failed"
            raise OSError(message)


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize("count", [42, 0, -1])
def test_real_adapter_insert_counts_final_statement_only(
    monkeypatch: pytest.MonkeyPatch, backend: str, count: int
) -> None:
    connection = CountConnection(backend, count)
    monkeypatch.setattr(execute_module, "get_sql_connection", lambda _key: connection)
    result = sql.execute_insert(
        backend,
        "public.target",
        "CREATE TABLE setup AS SELECT 1; SELECT 42",
        gp_break_query=True,
        return_metadata=True,
        retry_cnt=1,
    )
    expected = None if count < 0 else count
    assert result.rows == (expected or 0)
    assert result.metadata.affected_rows == expected
    assert result.metadata.inserted_rows == expected
    assert connection.closed == 1


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_execute_preserves_bare_return_shape_and_close_never_replays_success(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    connection = CountConnection(backend, 42, close_error=True)
    opened: list[str] = []

    def open_connection(key: str) -> CountConnection:
        opened.append(key)
        return connection

    monkeypatch.setattr(execute_module, "get_sql_connection", open_connection)
    assert sql.execute(backend, "INSERT INTO public.target SELECT 42", timeout_increment=0) is None
    assert opened == [backend]
    assert len(connection.queries) == 1


@pytest.mark.parametrize("failure", [None, ValueError("primary operation failure")])
def test_execute_read_connection_close_does_not_replay_or_mask_primary(
    monkeypatch: pytest.MonkeyPatch, failure: Exception | None
) -> None:
    class Connection(FakeClickHouseClient):
        def close(self) -> None:
            super().close()
            message = "close error"
            raise OSError(message)

        def query(self, *args: Any, **kwargs: Any) -> Any:
            if failure:
                raise failure
            return super().query(*args, **kwargs)

    connection = Connection(pd.DataFrame({"id": [1]}))
    opens: list[str] = []

    def opener(key: str) -> Connection:
        opens.append(key)
        return connection

    monkeypatch.setattr(read_module, "get_sql_connection", opener)
    if failure:
        with pytest.raises(ValueError, match="primary operation failure") as caught:
            sql.execute_read("ch", "SELECT 1", retry_cnt=1, timeout_increment=0)
        assert caught.value is failure
    else:
        assert sql.execute_read("ch", "SELECT 1", timeout_increment=0).iloc[0, 0] == 1
    assert opens == ["ch"]
    assert connection.close_calls == 1


def test_execute_read_adds_active_cancel_marker_without_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeClickHouseClient()
    monkeypatch.setattr(read_module, "get_sql_connection", lambda _key: connection)
    with activate_cancellation_scope(SqlCancellationScope()):
        markers = cancellation_query_comments()
        sql.execute_read("ch", "CREATE TEMPORARY TABLE temp (id UInt8); SELECT 1", retry_cnt=1)
    assert markers
    assert all(
        marker in query
        for marker in markers
        for query in [*connection.commands, *connection.read_queries]
    )


def test_trino_command_adapter_tolerates_a_cursor_without_fetchall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = CountConnection("gp", 42)
    connection.cursor_obj.fetchall = None
    monkeypatch.setattr(execute_module, "get_sql_connection", lambda _key: connection)
    assert sql.insert("trino", "sandbox.target", "SELECT 42", retry_cnt=1) == 42


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_execute_close_failures_preserve_the_primary_operation_error(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    connection = CountConnection(backend, 42, close_error=True)
    failure = ValueError("primary operation failure")
    connection.failure = failure
    monkeypatch.setattr(execute_module, "get_sql_connection", lambda _key: connection)
    with pytest.raises(ValueError, match="primary operation failure") as caught:
        sql.execute(backend, "INSERT INTO public.target SELECT 42", retry_cnt=1)
    assert caught.value is failure
    assert connection.closed == 1


def test_native_insert_format_lost_acknowledgement_replays_the_original_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = "INSERT INTO db.events FORMAT CSV\n9"
    accepted: list[str] = []
    raw_connections: list[Any] = []

    class Driver:
        closed = 0

        def execute(self, query: str) -> None:
            accepted.append(query)
            if len(accepted) == 1:
                message = "native INSERT acknowledgement lost"
                raise ConnectionError(message)

        def disconnect_connection(self) -> None:
            self.closed += 1

    def opener(_key: str) -> NativeClickHouseClient:
        driver = Driver()
        raw_connections.append(driver)
        return NativeClickHouseClient(driver)

    monkeypatch.setattr(execute_module, "get_sql_connection", opener)
    assert sql.execute("ch", payload, retry_cnt=2, timeout_increment=0) is None
    assert accepted == [payload, payload]
    assert all(driver.closed == 1 for driver in raw_connections)
