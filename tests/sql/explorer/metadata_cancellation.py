from __future__ import annotations

from threading import Event
from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.execution.cancellation import (
    AsyncSqlCancelled,
    SqlCancellationScope,
    activate_cancellation_scope,
    current_cancellation_scope,
)
from analytics_toolkit.sql.execution.metadata_cancellation import cancellable_metadata_connection
from analytics_toolkit.sql_explorer import completion
from analytics_toolkit.sql_explorer.completion import CompletionCoordinator, CompletionRequest

from tests.sql.explorer.completion import FakeProvider, _wait_for


def test_direct_metadata_calls_are_tagged_and_stopped() -> None:
    calls = []
    scope = SqlCancellationScope()

    def execute(statement: str, *args: object, **kwargs: object) -> str:
        calls.append((statement, args, kwargs))
        return "result"

    raw = SimpleNamespace(execute=execute, query=execute, command=execute, name="connection")
    raw.cursor = lambda: raw
    assert cancellable_metadata_connection(raw) is raw
    with activate_cancellation_scope(scope):
        connection = cancellable_metadata_connection(raw)
        assert connection.name == "connection"
        assert connection.cursor().execute("SELECT x", ("parameter",)) == "result"
        assert connection.query("DESCRIBE TABLE x", settings={}) == "result"
        assert connection.command("EXISTS TABLE x") == "result"
        assert all(scope.marker in statement for statement, _, _ in calls)
        scope.request_cancel()
        with pytest.raises(AsyncSqlCancelled):
            connection.cursor()
        with pytest.raises(AsyncSqlCancelled):
            connection.query("SELECT x")
    assert len(calls) == 3


def test_cancel_last_owner_stops_inflight_and_allows_same_scope_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release, cancelled = Event(), Event(), Event()
    scopes = []
    delivered = []

    class Provider(FakeProvider):
        def list_tables(self, **kwargs: object) -> tuple[str, ...]:
            scope = current_cancellation_scope()
            scopes.append(scope)
            entered.set()
            assert release.wait(2)
            return ("abcdef_one",)

    monkeypatch.setattr(completion, "cancel_scope_queries", lambda _: cancelled.set())
    coordinator = CompletionCoordinator("gp", "gp", provider=Provider())
    request = CompletionRequest("gp", "gp", "table", "abcdef")
    try:
        coordinator.enqueue(request, owner_id="1", on_success=delivered.append)
        assert entered.wait(2)
        coordinator.enqueue(request, owner_id="2", on_success=delivered.append)
        coordinator.remove_owner("1")
        assert not scopes[0].cancelled
        coordinator.remove_owner("2")
        assert scopes[0].cancelled
        assert cancelled.wait(2)
        coordinator.enqueue(request, owner_id="3", on_success=delivered.append)
        release.set()
        _wait_for(lambda: len(delivered) == 1)
        assert len(scopes) == 2
        assert not scopes[1].cancelled
        assert coordinator.cached(request) == ("abcdef_one",)
    finally:
        release.set()
        coordinator.stop()


def test_ddl_invalidation_drops_inflight_metadata_result() -> None:
    entered, release = Event(), Event()
    delivered = []

    class Provider(FakeProvider):
        def list_tables(self, **kwargs: object) -> tuple[str, ...]:
            entered.set()
            assert release.wait(2)
            return ("sample_old",)

    coordinator = CompletionCoordinator("gp", "gp", provider=Provider())
    request = CompletionRequest("gp", "gp", "table", "sample")
    try:
        coordinator.enqueue(request, owner_id="1", on_success=delivered.append)
        assert entered.wait(2)
        coordinator.invalidate_tables()
        release.set()
        _wait_for(lambda: not coordinator.snapshot()[2])
        assert coordinator.cached(request) is None
        assert delivered == []
    finally:
        release.set()
        coordinator.stop()
