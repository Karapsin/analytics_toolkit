"""Column metadata close failures preserve names and the primary query failure."""

from __future__ import annotations

from threading import Event
from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.metadata import column_names
from analytics_toolkit.sql_explorer.completion import CompletionCoordinator, CompletionRequest

from tests.sql._support.completion import FakeProvider, _wait_for


@pytest.mark.parametrize("failure", [None, "execute", "fetch"])
def test_column_metadata_cursor_close_cannot_replace_the_query_outcome(monkeypatch, failure):
    closed, results, errors = [], [], []
    primary = ConnectionError("primary column failure")

    def execute(statement):
        if failure == "execute":
            raise primary

    def fetch():
        if failure == "fetch":
            raise primary
        return []

    def close():
        closed.append("cursor")
        message = "cursor close failed"
        raise RuntimeError(message)

    cursor = SimpleNamespace(
        description=[("id",), ("name",)], execute=execute, fetchall=fetch, close=close
    )
    connection = SimpleNamespace(cursor=lambda: cursor, close=lambda: closed.append("connection"))
    monkeypatch.setattr(column_names, "get_sql_connection", lambda key: connection)
    coordinator = CompletionCoordinator("gp", "gp", provider=FakeProvider())
    ready = Event()
    request = CompletionRequest(
        "gp", "gp", "column", "", context="SELECT __explorer_cursor_column__ FROM t"
    )
    try:
        coordinator.enqueue(
            request,
            on_success=lambda result: (results.append(result.suggestions), ready.set()),
            on_error=lambda result, error: (errors.append(error), ready.set()),
        )
        assert ready.wait(2)
        if failure is None:
            assert results == [("id", "name")]
            assert errors == []
        else:
            assert results == []
            assert errors == [primary]
        assert closed == ["cursor", "connection"]
    finally:
        coordinator.stop()
        _wait_for(lambda: coordinator.is_stopped)
