"""Shared completion keeps every live owner attached to current configuration."""

from __future__ import annotations

import asyncio
from threading import Event

from analytics_toolkit.sql_explorer import background_metadata, completion
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.background_metadata import BackgroundMetadata
from analytics_toolkit.sql_explorer.completion import (
    CompletionCoordinatorPool,
    CompletionRequest,
    CompletionResult,
)
from analytics_toolkit.sql_explorer.metadata_listing import IncompleteMetadataError, MetadataListing
from analytics_toolkit.sql_explorer.metadata_store import MetadataStore

from tests.sql._support.completion import FakeProvider, _wait_for
from tests.sql._support.explorer import FakeSession


def test_background_notices_follow_live_pool_owners_after_first_owner_leaves(tmp_path, monkeypatch):
    entered, release = Event(), Event()

    class Provider(FakeProvider):
        def list_schemas(self, **kwargs):
            entered.set()
            assert release.wait(2)
            return MetadataListing((), complete=False)

    provider, delivered = Provider(), []
    monkeypatch.setattr(completion, "provider_for_backend", lambda backend: provider)

    def factory(directory, key, backend, source, on_error, **kwargs):
        return BackgroundMetadata(
            key,
            backend,
            source,
            MetadataStore(directory, "owners"),
            on_error=on_error,
            on_complete=kwargs.get("on_complete"),
        )

    monkeypatch.setattr(completion, "persistent_discovery", factory)
    pool = CompletionCoordinatorPool(state_directory=tmp_path)
    try:
        pool.acquire("gp", "gp", "first", on_error=lambda *args: delivered.append("first"))
        assert entered.wait(2)
        pool.acquire("gp", "gp", "second", on_error=lambda *args: delivered.append("second"))
        pool.release("first", "gp")
        release.set()
        _wait_for(lambda: bool(delivered))
        assert delivered == ["second"]
    finally:
        release.set()
        pool.stop()
        _wait_for(lambda: pool.is_stopped)


def test_configuration_replacement_rebinds_existing_tabs_and_preserves_their_ownership(monkeypatch):
    identity = ["initial"]
    monkeypatch.setattr(completion, "journal_context", lambda key: {"identity": identity[0]})
    monkeypatch.setattr(completion, "provider_for_backend", lambda backend: FakeProvider())

    async def exercise():
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            first = app.active_workspace
            old = first.completion
            request = CompletionRequest("gp", "gp", "table", "")
            old._store_result(request, ("previous_server",))
            identity[0] = "replacement"
            app.action_new_tab()
            await pilot.pause()
            second = app.active_workspace
            assert second is not first
            assert second.completion is not old
            assert first.completion is second.completion
            assert first.completion.cached(request) is None
            app._completion_pool.release(second.tab_id, "gp")
            assert app._completion_pool.coordinator_for("gp") is first.completion
            ready = Event()
            first.completion.enqueue(
                request, owner_id=first.tab_id, on_success=lambda result: ready.set()
            )
            assert ready.wait(2)
            assert first.completion.cached(request) is not None

    asyncio.run(exercise())


def _install_two_tab_discovery(tmp_path, monkeypatch):
    entered, release = Event(), Event()
    healthy, now, discoveries = [False], [100.0], {}
    monkeypatch.setattr(background_metadata, "time", lambda: now[0])

    class Provider(FakeProvider):
        def list_schemas(self, **kwargs):
            entered.set()
            assert release.wait(60)
            return MetadataListing(("sales",), complete=healthy[0])

    class EmptyProvider(FakeProvider):
        def list_catalogs(self, **kwargs):
            return ()

    provider = Provider()
    monkeypatch.setattr(
        completion,
        "provider_for_backend",
        lambda backend: provider if backend == "gp" else EmptyProvider(),
    )

    def factory(directory, key, backend, source, on_error, **kwargs):
        store = MetadataStore(directory, key)
        if key == "gp":
            store.save({("schema", "", ""): ("sales",), ("table", "", "sales"): ("saved",)})
        discovery = BackgroundMetadata(
            key,
            backend,
            source,
            store,
            on_error=on_error,
            on_complete=kwargs.get("on_complete"),
        )
        if key == "gp":
            discovery.ledger.consume(1, 99, {("schema", "", ""): "ddl"}, [])
        discoveries[key] = discovery
        return discovery

    monkeypatch.setattr(completion, "persistent_discovery", factory)
    return entered, release, healthy, now, discoveries


def _observe_recovery(app, workspace, coordinator, monkeypatch):
    failed, recovered = asyncio.Event(), asyncio.Event()
    observed_errors = []
    receive_error = app._receive_metadata_error
    receive_complete = app._receive_metadata_complete

    def error(tab_id, exc, epoch=None, origin=None):
        receive_error(tab_id, exc, epoch, origin)
        if tab_id == workspace.tab_id and origin is coordinator:
            observed_errors.append(exc)
            failed.set()

    def complete(tab_id, origin=None):
        was_incomplete = workspace.metadata_incomplete_notice
        receive_complete(tab_id, origin)
        if (
            tab_id == workspace.tab_id
            and was_incomplete
            and not workspace.metadata_incomplete_notice
        ):
            recovered.set()

    monkeypatch.setattr(app, "_receive_metadata_error", error)
    monkeypatch.setattr(app, "_receive_metadata_complete", complete)
    return failed, recovered, observed_errors


def test_two_tab_background_error_and_recovery_keep_the_actual_producer_after_alias_switch(
    tmp_path, monkeypatch
):
    entered, release, healthy, now, discoveries = _install_two_tab_discovery(tmp_path, monkeypatch)

    async def exercise():
        session = FakeSession()
        session.explorer_state_dir = tmp_path
        app = SqlExplorerApp(session)
        async with app.run_test() as pilot:
            assert await asyncio.to_thread(entered.wait, 10)
            first = app.active_workspace
            original = first.completion
            app.action_new_tab()
            await pilot.pause()
            second = app.active_workspace
            assert second.completion is original
            failed, recovered, observed_errors = _observe_recovery(
                app, second, original, monkeypatch
            )
            app._activate_tab(first.tab_id)
            app._command_database(["other"])
            await pilot.pause()
            switched_notice = str(first.query_one("#notice").renderable)
            assert first.completion is not original
            release.set()
            await asyncio.wait_for(failed.wait(), 10)
            assert second.metadata_incomplete_notice
            assert "Metadata is incomplete" in str(second.query_one("#notice").renderable)
            assert str(first.query_one("#notice").renderable) == switched_notice
            assert not first.metadata_incomplete_notice
            app._metadata_state_from_thread(
                first.tab_id,
                original,
                CompletionResult(CompletionRequest("gp", "gp", "schema", ""), 0, ()),
                IncompleteMetadataError("late original producer"),
            )
            assert str(first.query_one("#notice").renderable) == switched_notice
            assert not first.metadata_incomplete_notice
            healthy[0], now[0] = True, 160.0
            discoveries["gp"].invalidate()
            await asyncio.wait_for(recovered.wait(), 10)
            assert str(second.query_one("#notice").renderable) == ""
            assert str(first.query_one("#notice").renderable) == switched_notice
            app._metadata_state_from_thread(
                second.tab_id,
                original,
                CompletionResult(CompletionRequest("gp", "gp", "schema", ""), 0, ()),
                observed_errors[0],
            )
            assert not second.metadata_incomplete_notice
            assert str(second.query_one("#notice").renderable) == ""

    try:
        asyncio.run(exercise())
    finally:
        release.set()
