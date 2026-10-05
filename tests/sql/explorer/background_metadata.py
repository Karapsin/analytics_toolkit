from __future__ import annotations

from dataclasses import replace
from threading import Event
from time import time
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit.sql.execution.cancellation import AsyncSqlCancelled
from analytics_toolkit.sql_explorer import background_metadata as module
from analytics_toolkit.sql_explorer import completion
from analytics_toolkit.sql_explorer.background_metadata import (
    BackgroundMetadata,
    persistent_discovery,
)
from analytics_toolkit.sql_explorer.completion import (
    CompletionCoordinator,
    CompletionCoordinatorPool,
    CompletionRequest,
)
from analytics_toolkit.sql_explorer.metadata_store import MetadataStore

from tests.sql.explorer.completion import FakeProvider, _wait_for

if TYPE_CHECKING:
    from pathlib import Path


class Provider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.names = ("orders", "orders_old")
        self.release = Event()
        self.started = Event()
        self.fail = False

    def list_schemas(self, **kwargs: object) -> tuple[str, ...]:
        return ("public", "empty")

    def list_tables(self, **kwargs: object) -> tuple[str, ...]:
        self.started.set()
        assert self.release.wait(3)
        if self.fail:
            message = "metadata unavailable"
            raise RuntimeError(message)
        return self.names if kwargs["schema"] == "public" else ()


def test_relaunch_reuses_completed_snapshots_without_rescanning(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path, "gp")
    store.save({("schema", "", ""): ("public",), ("table", "", "public"): ("old",)})
    provider = Provider()
    errors: list[Exception] = []
    for _ in range(2):
        discovery = BackgroundMetadata("gp", "gp", provider, store, on_error=errors.append)
        assert discovery.cached("table", schema="public") == ("old",)
        assert discovery._discover(0) is False
        assert not provider.started.is_set()
    assert errors == []


def test_interactive_worker_runs_while_background_is_blocked(tmp_path: Path) -> None:
    provider = Provider()
    discovery = BackgroundMetadata("gp", "gp", provider, None, on_error=lambda exc: None)
    coordinator = CompletionCoordinator("gp", "gp", provider=FakeProvider(), discovery=discovery)
    finished = Event()
    discovery.start()
    try:
        assert provider.started.wait(2)
        coordinator.enqueue(
            CompletionRequest("gp", "gp", "table", "sample", schema="missing"),
            on_success=lambda result: finished.set(),
        )
        assert finished.wait(2)
        assert not provider.release.is_set()
    finally:
        provider.release.set()
        coordinator.stop()
        _wait_for(lambda: coordinator.is_stopped)


def test_failed_refresh_and_invalidation_retain_saved_names(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path, "gp")
    store.save({("schema", "", ""): ("public",), ("table", "", "public"): ("saved",)})
    provider = Provider()
    provider.fail = True
    provider.release.set()
    errors: list[Exception] = []
    discovery = BackgroundMetadata("gp", "gp", provider, store, on_error=errors.append)
    discovery.ledger.consume(1, time() + 1, {("table", "", "public"): "ddl"}, [])
    discovery.start()
    try:
        _wait_for(lambda: len(errors) == 1)
        discovery.invalidate()
        assert discovery.cached("table", schema="public") == ("saved",)
        assert store.load()[("table", "", "public")] == ("saved",)
        assert discovery._discover(0) is False
    finally:
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)


def test_complete_namespace_removes_dropped_schemas_and_catalogs(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path, "trino")
    store.save(
        {("table", "deleted", "public"): ("gone",), ("table", "iceberg", "deleted"): ("gone",)}
    )
    provider = Provider()
    provider.release.set()
    errors: list[Exception] = []
    discovery = BackgroundMetadata(
        "trino",
        "trino",
        provider,
        store,
        default_catalog="iceberg",
        on_error=errors.append,
    )
    discovery.start()
    try:
        _wait_for(
            lambda: (
                discovery.cached("table", "hive", "empty") == ()
                and discovery.cached("table") == provider.names
            )
        )
        snapshots = store.load()
        assert ("table", "deleted", "public") not in snapshots
        assert ("table", "iceberg", "deleted") not in snapshots
        assert discovery.cached("table") == provider.names
        assert errors == []
    finally:
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)


def test_partial_namespace_does_not_mask_live_fallback_and_stale_publish_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MetadataStore(tmp_path, "gp")
    errors: list[Exception] = []
    discovery = BackgroundMetadata("gp", "gp", Provider(), store, on_error=errors.append)
    coordinator = CompletionCoordinator("gp", "gp", provider=FakeProvider(), discovery=discovery)
    try:
        assert discovery.cached("table") is None
        assert coordinator.cached_schemas() is None
        discovery._publish(("schema", "", ""), ("public",), 0)
        assert coordinator.cached_schemas() == ("public",)
        assert coordinator.known_catalogs() is None
        assert discovery.cached("table") is None

        discovery._generation += 1
        discovery._publish(("table", "", "public"), ("late",), 0)
        assert discovery.cached("table", schema="public") is None
        assert not errors
    finally:
        coordinator.stop()
        _wait_for(lambda: coordinator.is_stopped)


def test_failed_schema_listing_retains_snapshot_and_continues_other_catalogs(
    tmp_path: Path,
) -> None:
    class PartialProvider(Provider):
        def list_schemas(self, **kwargs):
            if kwargs["catalog"] == "hive":
                message = "hive unavailable"
                raise RuntimeError(message)
            return super().list_schemas(**kwargs)

    provider = PartialProvider()
    provider.release.set()
    store = MetadataStore(tmp_path, "trino")
    store.save({("schema", "hive", ""): ("saved",)})
    errors: list[Exception] = []
    discovery = BackgroundMetadata("trino", "trino", provider, store, on_error=errors.append)
    discovery.ledger.consume(1, time() + 1, {("schema", "hive", ""): "ddl"}, [])
    discovery.start()
    try:
        _wait_for(
            lambda: (
                len(errors) == 1
                and discovery.cached("table", "iceberg", "public") == provider.names
            )
        )
        assert discovery.cached("schema", "hive") == ("saved",)
        assert discovery.cached("table", "iceberg", "public") == provider.names
    finally:
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)


def test_schema_listing_cancellation_stops_without_error_notice() -> None:
    stopped = Event()

    class CancelledProvider(Provider):
        def list_schemas(self, **kwargs):
            stopped.set()
            raise AsyncSqlCancelled

    errors: list[Exception] = []
    discovery = BackgroundMetadata("gp", "gp", CancelledProvider(), None, on_error=errors.append)
    discovery.start()
    try:
        assert stopped.wait(2)
    finally:
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)
    assert errors == []


@pytest.mark.parametrize("cancelled", [False, True])
def test_catalog_errors_are_reported_unless_shutdown_cancelled_request(cancelled: bool) -> None:
    started, release = Event(), Event()

    class BrokenCatalog(Provider):
        def list_catalogs(self, **kwargs):
            started.set()
            assert release.wait(2)
            message = "catalog unavailable"
            raise RuntimeError(message)

    errors: list[Exception] = []
    discovery = BackgroundMetadata("trino", "trino", BrokenCatalog(), None, on_error=errors.append)
    discovery.start()
    try:
        assert started.wait(2)
        if cancelled:
            discovery.stop()
        release.set()
        if not cancelled:
            _wait_for(lambda: len(errors) == 1)
    finally:
        release.set()
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)
    assert len(errors) == (0 if cancelled else 1)


def test_pool_configuration_failure_keeps_interactive_completion_available(tmp_path: Path) -> None:
    pool = CompletionCoordinatorPool(state_directory=tmp_path)
    errors: list[Exception] = []
    try:
        first = pool.acquire(
            "missing", "gp", "one", on_error=lambda result, exc: errors.append(exc)
        )
        assert first.discovery is None
        assert errors
        assert pool.acquire("another_missing", "gp", "two").discovery is None
        first.provider = FakeProvider()
        ready = Event()
        first.enqueue(
            CompletionRequest("missing", "gp", "table", "orders"),
            on_success=lambda result: ready.set(),
        )
        assert ready.wait(2)
    finally:
        pool.stop()
        _wait_for(lambda: pool.is_stopped)


def test_unqualified_snapshots_preserve_case_insensitive_deduplication(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path, "gp")
    store.save(
        {
            ("schema", "", ""): ("first", "second"),
            ("table", "", "first"): ("Orders", "zebra"),
            ("table", "", "second"): ("orders", "Alpha"),
        }
    )
    discovery = BackgroundMetadata("gp", "gp", Provider(), store, on_error=lambda exc: None)
    assert discovery.cached("table") == ("Alpha", "Orders", "zebra")


def test_connection_fingerprint_changes_and_does_not_store_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    config = module.get_connection_config("gp")
    first = persistent_discovery(tmp_path, "gp", "gp", Provider(), lambda exc: None)
    assert first.store is not None
    first.store.save({("table", "", "public"): ("orders",)})
    monkeypatch.setattr(module, "get_connection_config", lambda key: replace(config, host="new"))
    second = persistent_discovery(tmp_path, "gp", "gp", Provider(), lambda exc: None)
    assert second.cached("table", schema="public") is None
    assert b"password" not in first.store.path.read_bytes()
    assert config.host.encode() not in first.store.path.read_bytes()


def test_pool_shares_background_worker_and_stops_on_last_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    provider = Provider()
    provider.release.set()
    monkeypatch.setattr(completion, "provider_for_backend", lambda backend: provider)
    pool = CompletionCoordinatorPool(state_directory=tmp_path)
    try:
        first = pool.acquire("gp", "gp", "one")
        assert pool.acquire("GP", "gp", "two") is first
        _wait_for(lambda: first.discovery.cached("table", schema="empty") == ())
        pool.release("one")
        assert pool.coordinator_for("gp") is first
        pool.release("two")
        _wait_for(lambda: pool.is_stopped)
    finally:
        pool.stop()


def test_cache_and_subscriber_context_are_independent() -> None:
    provider = FakeProvider()
    coordinator = CompletionCoordinator("gp", "gp", provider=provider)
    request = CompletionRequest("gp", "gp", "table", "sample", context="from:0")
    completed = Event()
    try:
        coordinator.enqueue(request, on_success=lambda result: completed.set())
        assert completed.wait(2)
        moved = replace(request, context="join:48", prefix="sample_o")
        assert coordinator.cached(moved) == ("sample_one",)
        assert len(provider.table_calls) == 1
    finally:
        coordinator.stop()


def test_corrupt_store_and_unwritable_save_keep_in_memory_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MetadataStore(tmp_path, "gp")
    store.path.write_text("corrupt")
    errors: list[Exception] = []
    provider = Provider()
    provider.release.set()
    discovery = BackgroundMetadata("gp", "gp", provider, store, on_error=errors.append)
    assert errors

    def fail_save(*args):
        message = "read only"
        raise OSError(message)

    monkeypatch.setattr(store, "save", fail_save)
    monkeypatch.setattr(store, "update", fail_save)
    discovery.start()
    try:
        _wait_for(
            lambda: (
                discovery.cached("table", schema="empty") == ()
                and discovery.cached("table", schema="public") == provider.names
            )
        )
        assert discovery.cached("table", schema="public") == provider.names
        assert discovery.store is None
    finally:
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)
