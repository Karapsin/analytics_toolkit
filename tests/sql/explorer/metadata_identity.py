from __future__ import annotations

import asyncio
from dataclasses import replace
from threading import Event
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit.sql.execution.backend_options import current_backend_operation
from analytics_toolkit.sql.execution.cancellation import current_cancellation_scope
from analytics_toolkit.sql.execution.plans import SqlOperationMetadata, SqlOperationResult
from analytics_toolkit.sql_explorer import background_metadata, completion
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.background_metadata import BackgroundMetadata
from analytics_toolkit.sql_explorer.completion import (
    CompletionCoordinator,
    CompletionCoordinatorPool,
    CompletionRequest,
    CompletionResult,
)
from analytics_toolkit.sql_explorer.identifier_completion import (
    identifier_tokens,
    quote_completion_name,
)
from analytics_toolkit.sql_explorer.metadata_ledger import MetadataLedger
from analytics_toolkit.sql_explorer.metadata_listing import IncompleteMetadataError, MetadataListing
from analytics_toolkit.sql_explorer.metadata_store import MetadataStore
from sqlglot.errors import SqlglotError

from tests.sql._support.completion import FakeProvider, _install_stub, _wait_for
from tests.sql._support.explorer import FakeSession

if TYPE_CHECKING:
    from pathlib import Path


def test_exact_names_survive_sql_result_and_sqlite_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = ("Events", "events", " space ", "Events", "select")
    metadata = SqlOperationMetadata(data_complete=False, ch_cluster_status=[{"available": False}])
    monkeypatch.setattr(
        completion.sql,
        "read",
        lambda *args, **kwargs: SqlOperationResult(
            rows=len(names), data=pd.DataFrame({"name": names}), metadata=metadata
        ),
    )
    values = completion.ClickHouseCompletionProvider().list_schemas(connection_key="ch")
    assert values == (" space ", "Events", "events", "select")
    assert values.complete is False
    assert values.cluster_status == ({"available": False},)
    store = MetadataStore(tmp_path, "v2")
    store.save({("table", "", "current"): tuple(values)})
    assert store.load()[("table", "", "current")] == tuple(values)


@pytest.mark.parametrize("name", ["Events", " space ", "select", "a.b", "end.", 'a"b'])
def test_metadata_identifier_insertion_replaces_entire_quoted_fragment(name: str) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            _install_stub(app)
            workspace = app.active_workspace
            editor = workspace.editor
            for fragment in ('"Ev', '"Ev"'):
                editor.text = "select * from analytics." + fragment
                editor.cursor_location = (0, len(editor.text))
                context = app._completion_at_cursor(workspace)
                assert context.request.kind == "table"
                assert context.request.schema == "analytics"
                assert editor.text[context.replacement_start :] == fragment
                app._insert_completion(context, name, workspace)
                assert (
                    editor.text
                    == "select * from analytics." + quote_completion_name(name, "gp") + " "
                )
            editor.text = 'select * from analytics."EventsOld"'
            editor.cursor_location = (0, editor.text.index("EventsOld") + 2)
            context = app._completion_at_cursor(workspace)
            app._insert_completion(context, name, workspace)
            assert (
                editor.text == "select * from analytics." + quote_completion_name(name, "gp") + " "
            )

    asyncio.run(exercise())


def test_reserved_column_metadata_is_quoted() -> None:
    values = completion.projection_suggestions(
        "SELECT __explorer_cursor_column__ FROM sales.orders", "gp", lambda table: ("select", "id")
    )
    assert values == ('"select"', "id")


@pytest.mark.parametrize(
    ("backend", "quote"), [("gp", '"'), ("ch", '"'), ("ch", "`"), ("trino", '"')]
)
def test_quoted_fragments_preserve_exact_prefix_and_dotted_namespace(
    backend: str, quote: str
) -> None:
    text = f"select * from {quote}db.with.dot{quote}.{quote} space"
    context = completion.parse_completion_context(text, len(text), backend=backend)
    assert context.request.kind == "table"
    assert context.request.schema == "db.with.dot"
    assert context.request.prefix == " space"
    assert text[context.replacement_start :] == quote + " space"
    assert completion.filter_suggestions((" space ", "space"), context.request.prefix) == (
        " space ",
    )


def test_persisted_unqualified_clickhouse_completion_uses_current_database() -> None:
    discovery = BackgroundMetadata(
        "ch",
        "ch",
        FakeProvider(),
        None,
        on_error=lambda error: None,
        context={"database": "current"},
    )
    try:
        discovery._snapshots = {
            ("schema", "", ""): ("current", "remote"),
            ("table", "", "current"): ("Events", "events"),
            ("table", "", "remote"): ("remote_only",),
        }
        assert discovery.cached("table") == ("Events", "events")
        assert discovery.cached("table", schema="remote") == ("remote_only",)
    finally:
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)


def test_partial_catalog_never_prunes_complete_snapshot_and_recovers_without_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Provider(FakeProvider):
        complete = False

        def list_schemas(self, **kwargs: object) -> MetadataListing:
            return MetadataListing(("current",), complete=self.complete)

    store = MetadataStore(tmp_path, "ch")
    store.save(
        {
            ("schema", "", ""): ("current", "remote"),
            ("table", "", "current"): (),
            ("table", "", "remote"): ("keep",),
        }
    )
    errors = []
    provider = Provider()
    recovered = []
    discovery = BackgroundMetadata(
        "ch",
        "ch",
        provider,
        store,
        on_error=errors.append,
        on_complete=lambda: recovered.append(True),
    )
    now = [100.0]
    monkeypatch.setattr(background_metadata, "time", lambda: now[0])
    discovery.ledger.consume(1, 99, {("schema", "", ""): "ddl"}, [])
    try:
        assert discovery._discover(0)
        assert isinstance(errors[0], IncompleteMetadataError)
        assert discovery.cached("schema") == ("current", "remote")
        assert store.load()[("table", "", "remote")] == ("keep",)
        assert not discovery._discover(0)
        provider.complete = True
        now[0] += 60
        assert discovery._discover(0)
        assert discovery.cached("schema") == ("current",)
        assert ("table", "", "remote") not in store.load()
        assert discovery.incomplete == set()
        assert recovered == [True]
    finally:
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)


def test_failed_completed_scan_retries_without_an_additional_usage_event() -> None:
    ledger = MetadataLedger(None)
    key = ("schema", "", "")
    try:
        ledger.ensure([key])
        ticket = ledger.claim(10)
        assert ticket is not None
        assert ledger.finish(ticket, ("old",), 10)
        ledger.consume(1, 11, {key: "ddl"}, [])
        ticket = ledger.claim(12)
        assert ticket is not None
        ledger.failed(ticket, 12)
        assert ledger.claim(71) is None
        assert ledger.claim(72) is not None
    finally:
        ledger.close()


def test_interactive_partial_result_keeps_previous_complete_names() -> None:
    class Provider(FakeProvider):
        def list_tables(self, **kwargs: object) -> MetadataListing:
            return MetadataListing(("partial",), complete=False)

    coordinator = CompletionCoordinator("gp", "gp", provider=Provider())
    request = CompletionRequest("gp", "gp", "table", "")
    errors, ready = [], Event()
    try:
        coordinator._store_result(request, ("Events", "events"))
        entry = coordinator._cache[request.cache_key]
        coordinator._cache[request.cache_key] = replace(entry, created_at=entry.created_at - 61)
        coordinator.enqueue(
            request, on_error=lambda result, error: (errors.append(error), ready.set())
        )
        assert ready.wait(2)
        assert isinstance(errors[0], IncompleteMetadataError)
        assert coordinator.cached(request) == ("Events", "events")
    finally:
        coordinator.stop()
        _wait_for(lambda: coordinator.is_stopped)


@pytest.mark.parametrize("superseded", ["cancelled", "generation"])
def test_superseded_discovery_never_commits_a_snapshot(tmp_path: Path, superseded: str) -> None:
    class Provider(FakeProvider):
        def list_schemas(self, **kwargs: object) -> MetadataListing:
            if superseded == "cancelled":
                discovery._scope.request_cancel()
            else:
                discovery._generation += 1
            return MetadataListing(("late",))

    store = MetadataStore(tmp_path, "gp")
    discovery = BackgroundMetadata("gp", "gp", Provider(), store, on_error=lambda error: None)
    try:
        assert discovery._discover(0)
        assert store.load() == {}
        assert discovery.cached("schema") is None
    finally:
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)


def test_incomplete_notice_clears_only_after_all_partial_scopes_recover() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            coordinator = _install_stub(app)
            workspace = app.active_workspace
            coordinator.metadata_incomplete = True
            app._receive_metadata_error(workspace.tab_id, IncompleteMetadataError("partial"))
            assert workspace.metadata_incomplete_notice
            app._receive_metadata_complete(workspace.tab_id)
            assert workspace.metadata_incomplete_notice
            coordinator.metadata_incomplete = False
            app._receive_metadata_complete(workspace.tab_id)
            assert not workspace.metadata_incomplete_notice

    asyncio.run(exercise())


def test_clickhouse_current_database_is_resolved_before_cached_table_use() -> None:
    class Provider(FakeProvider):
        def list_schemas(self, **kwargs: object) -> tuple[str, ...]:
            return ("current", "other")

        def resolve_reference(self, *args: object, **kwargs: object) -> tuple[str, str]:
            return "", "current"

    discovery = BackgroundMetadata("ch", "ch", Provider(), None, on_error=lambda error: None)
    try:
        discovery._snapshots[("table", "", "current")] = ("local",)
        discovery._snapshots[("table", "", "other")] = ("remote",)
        assert discovery.cached("table") is None
        assert discovery._discover(0)
        assert discovery.cached("table") == ("local",)
        assert discovery.context["database"] == "current"
    finally:
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)


def test_partial_columns_retain_certified_names_until_complete_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial = [True]

    def columns(*args: object) -> tuple[str, ...]:
        operation = current_backend_operation()
        assert operation is not None
        operation.observed_partial = partial[0]
        return ("partial",) if partial[0] else ("recovered",)

    monkeypatch.setattr(completion, "table_column_names", columns)
    coordinator = CompletionCoordinator("ch", "ch", provider=FakeProvider())
    request = CompletionRequest(
        "ch", "ch", "column", "", context="SELECT __explorer_cursor_column__ FROM t"
    )
    errored, ready = Event(), Event()
    try:
        coordinator._table_columns["t"] = ("certified",)
        coordinator._columns_cached_at["t"] = 0
        coordinator._store_result(request, ("certified",))
        entry = coordinator._cache[request.cache_key]
        coordinator._cache[request.cache_key] = replace(entry, created_at=entry.created_at - 61)
        coordinator.enqueue(request, on_error=lambda *args: errored.set())
        assert errored.wait(2)
        assert coordinator.metadata_incomplete
        assert coordinator.cached(request) == ("certified",)
        assert coordinator._table_columns["t"] == ("certified",)
        partial[0] = False
        entry = coordinator._cache[request.cache_key]
        coordinator._cache[request.cache_key] = replace(entry, created_at=entry.created_at - 61)
        _wait_for(lambda: coordinator.snapshot()[2] is False)
        coordinator.enqueue(request, on_success=lambda *args: ready.set())
        assert ready.wait(2)
        assert coordinator.cached(request) == ("recovered",)
        assert not coordinator.metadata_incomplete
    finally:
        coordinator.stop()
        _wait_for(lambda: coordinator.is_stopped)


def test_partial_first_listing_preserves_unrelated_cache_and_reports_incompleteness() -> None:
    class Provider(FakeProvider):
        def list_tables(self, **kwargs: object) -> MetadataListing:
            return MetadataListing(("partial",), complete=False)

    discovery = BackgroundMetadata("ch", "ch", Provider(), None, on_error=lambda error: None)
    coordinator = CompletionCoordinator("ch", "ch", provider=Provider(), discovery=discovery)
    request = CompletionRequest("ch", "ch", "table", "")
    unrelated = CompletionRequest("ch", "ch", "schema", "")
    ready, successes = Event(), []
    try:
        coordinator._store_result(unrelated, ("complete",))
        prior = coordinator._cache[unrelated.cache_key]
        coordinator.enqueue(
            request, on_success=successes.append, on_error=lambda *args: ready.set()
        )
        assert ready.wait(2)
        assert successes == []
        assert coordinator.cached(request) is None
        assert coordinator._cache[unrelated.cache_key] is prior
        assert coordinator.metadata_incomplete
        coordinator._incomplete_scopes.clear()
        discovery.incomplete.add(("schema", "", ""))
        assert coordinator.metadata_incomplete
        discovery.incomplete.clear()
        assert not coordinator.metadata_incomplete
    finally:
        coordinator.stop()
        _wait_for(lambda: coordinator.is_stopped)


def test_configuration_change_replaces_coordinator_without_reusing_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ["first"]
    monkeypatch.setattr(completion, "journal_context", lambda key: {"identity": identity[0]})
    monkeypatch.setattr(completion, "provider_for_backend", lambda backend: FakeProvider())
    pool = CompletionCoordinatorPool()
    try:
        first = pool.acquire("ch", "ch", "first-owner")
        first._store_result(CompletionRequest("ch", "ch", "schema", ""), ("old",))
        identity[0] = "replacement"
        replacement = pool.acquire("ch", "ch", "second-owner")
        assert replacement is not first
        assert replacement.cached(CompletionRequest("ch", "ch", "schema", "")) is None
        _wait_for(lambda: first.is_stopped)
    finally:
        pool.stop()
        _wait_for(lambda: pool.is_stopped)


def test_successful_current_completion_clears_prior_incomplete_notice() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            coordinator = _install_stub(app)
            coordinator.table_cache = ("sample",)
            workspace = app.active_workspace
            workspace.editor.text = "select * from sam"
            workspace.editor.cursor_location = (0, len(workspace.editor.text))
            workspace.completion_requested_text = workspace.editor.text
            workspace.completion_cursor = workspace.editor.cursor_location
            workspace.metadata_incomplete_notice = True
            request = app._completion_at_cursor(workspace).request
            app._receive_completion(CompletionResult(request, 1, ("sample",)), workspace.tab_id)
            assert not workspace.metadata_incomplete_notice
            assert workspace.editor.text == "select * from sample "

    asyncio.run(exercise())


def test_quoted_relation_still_replaces_closing_quote_when_later_sql_is_unfinished() -> None:
    text = 'select * from "Ev" where value = \'unfinished'
    cursor = text.index('" where')
    context = completion.parse_completion_context(text, cursor, backend="gp")
    assert context.request.kind == "table"
    assert text[context.replacement_start : context.replacement_end] == '"Ev"'


def test_unclosed_string_is_not_recovered_as_a_quoted_identifier() -> None:
    with pytest.raises(SqlglotError):
        identifier_tokens('"unfinished', "hive")


@pytest.mark.parametrize("superseded", ["cancelled", "generation"])
def test_partial_column_failure_cannot_publish_after_request_is_superseded(
    monkeypatch: pytest.MonkeyPatch, superseded: str
) -> None:
    fetched = Event()

    def columns(*args: object) -> tuple[str, ...]:
        operation = current_backend_operation()
        assert operation is not None
        operation.observed_partial = True
        if superseded == "cancelled":
            scope = current_cancellation_scope()
            assert scope is not None
            scope.request_cancel()
        else:
            coordinator.invalidate_tables(rediscover=False)
        fetched.set()
        return ("partial",)

    monkeypatch.setattr(completion, "table_column_names", columns)
    coordinator = CompletionCoordinator("ch", "ch", provider=FakeProvider())
    request = CompletionRequest(
        "ch", "ch", "column", "", context="SELECT __explorer_cursor_column__ FROM t"
    )
    errors = []
    try:
        coordinator.enqueue(request, on_error=lambda result, error: errors.append(error))
        assert fetched.wait(2)
        _wait_for(lambda: coordinator.snapshot()[2] is False)
        assert not coordinator.metadata_incomplete
        assert coordinator.cached(request) is None
        assert errors == []
    finally:
        coordinator.stop()
        _wait_for(lambda: coordinator.is_stopped)
