"""Metadata certification survives recovery while stale workers cannot publish."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from threading import Event

import pandas as pd
import pytest
import sqlglot
from analytics_toolkit.sql.execution.cancellation import current_cancellation_scope
from analytics_toolkit.sql_explorer import background_metadata, completion
from analytics_toolkit.sql_explorer.background_metadata import (
    BackgroundMetadata,
    persistent_discovery,
)
from analytics_toolkit.sql_explorer.completion import CompletionCoordinator, CompletionRequest
from analytics_toolkit.sql_explorer.metadata_listing import MetadataListing
from analytics_toolkit.sql_explorer.metadata_store import MetadataStore

from tests.sql._support.completion import FakeProvider, _wait_for


@pytest.mark.parametrize("failure", ["partial", "connection"])
def test_refresh_failure_remains_incomplete_across_restart_until_complete_retry(
    tmp_path, monkeypatch, failure
):
    class Provider(FakeProvider):
        failed = True

        def list_tables(self, **kwargs):
            if self.failed and failure == "connection":
                message = "discovery unavailable"
                raise ConnectionError(message)
            return MetadataListing(("new",), complete=not self.failed)

    key = ("table", "", "sales")
    store = MetadataStore(tmp_path, "gp")
    store.save({("schema", "", ""): ("sales",), key: ("saved",)})
    now = [100.0]
    monkeypatch.setattr(background_metadata, "time", lambda: now[0])
    provider, errors, recovered = Provider(), [], []
    first = BackgroundMetadata("gp", "gp", provider, store, on_error=errors.append)
    first.ledger.consume(1, 99, {key: "ddl"}, [])
    try:
        assert first._discover(0)
        assert first.incomplete == {key}
        assert first.cached("table", schema="sales") == ("saved",)
        first.stop()
        second = BackgroundMetadata(
            "gp",
            "gp",
            provider,
            store,
            on_error=errors.append,
            on_complete=lambda: recovered.append(True),
        )
        try:
            assert second.incomplete == {key}
            assert second.cached("table", schema="sales") == ("saved",)
            assert not second._discover(0)
            provider.failed = False
            now[0] += 60
            assert second._discover(0)
            assert second.cached("table", schema="sales") == ("new",)
            assert second.incomplete == set()
            assert recovered == [True]
        finally:
            second.stop()
    finally:
        first.stop()


@pytest.mark.parametrize("failure", ["partial", "connection"])
def test_complete_parent_removal_retires_failed_descendant_and_its_retry(
    tmp_path, monkeypatch, failure
):
    class Provider(FakeProvider):
        def list_schemas(self, **kwargs):
            if failure == "connection":
                message = "catalog unavailable"
                raise ConnectionError(message)
            return MetadataListing((), complete=False)

        def list_catalogs(self, **kwargs):
            return ("lake",)

    store = MetadataStore(tmp_path, "trino")
    store.save({("catalog", "", ""): ("hive", "lake")})
    discovery = BackgroundMetadata("trino", "trino", Provider(), store, on_error=lambda exc: None)
    try:
        assert discovery._discover(0)
        discovery.ledger.consume(1, 100, {("catalog", "", ""): "ddl"}, [])
        assert discovery._discover(0)
        assert ("schema", "hive", "") not in discovery.incomplete
        with discovery.ledger.transaction() as connection:
            remaining = connection.execute(
                "SELECT kind,catalog,schema_name FROM scan_state WHERE identity=?",
                (discovery.ledger.identity,),
            ).fetchall()
        assert ("schema", "hive", "") not in [tuple(row) for row in remaining]
        assert discovery.cached("catalog") == ("lake",)
    finally:
        discovery.stop()


def test_new_background_certification_clears_interactive_partial_but_old_cache_does_not(tmp_path):
    class Provider(FakeProvider):
        complete = False

        def list_tables(self, **kwargs):
            return MetadataListing(("recovered",), complete=self.complete)

    store = MetadataStore(tmp_path, "gp")
    store.save({("schema", "", ""): ("sales",)})
    provider = Provider()
    discovery = BackgroundMetadata("gp", "gp", provider, store, on_error=lambda exc: None)
    coordinator = CompletionCoordinator("gp", "gp", provider=provider, discovery=discovery)
    request = CompletionRequest("gp", "gp", "table", "", schema="sales")
    errored = Event()
    try:
        coordinator._store_result(request, ("saved",))
        entry = coordinator._cache[request.cache_key]
        coordinator._cache[request.cache_key] = replace(entry, created_at=entry.created_at - 61)
        coordinator.enqueue(request, on_error=lambda *args: errored.set())
        assert errored.wait(2)
        assert coordinator.cached(request) == ("saved",)
        assert coordinator.metadata_incomplete
        provider.complete = True
        assert discovery._discover(0)
        assert coordinator.cached(request) == ("recovered",)
        assert not coordinator.metadata_incomplete
    finally:
        coordinator.stop()
        _wait_for(lambda: coordinator.is_stopped)


def test_changed_resolved_alias_cannot_certify_names_in_the_original_sqlite_identity(
    tmp_path, monkeypatch
):
    config = [background_metadata.get_connection_config("gp")]
    monkeypatch.setattr(background_metadata, "get_connection_config", lambda key: config[0])

    class Provider(FakeProvider):
        def list_schemas(self, **kwargs):
            config[0] = replace(config[0], host="replacement-host")
            return ("replacement_names",)

    errors = []
    discovery = persistent_discovery(tmp_path, "gp", "gp", Provider(), errors.append)
    assert discovery.store is not None
    store = discovery.store
    store.save({("schema", "", ""): ("original_names",)})
    discovery.ledger.consume(1, 100, {("schema", "", ""): "ddl"}, [])
    try:
        assert discovery._discover(0)
        assert store.load()[("schema", "", "")] == ("original_names",)
        assert errors
    finally:
        discovery.stop()


@pytest.mark.parametrize("superseded", ["generation", "cancelled"])
def test_superseded_ordinary_metadata_failure_cannot_reach_subscribers(superseded):
    entered, release = Event(), Event()
    scopes, errors = [], []

    class Provider(FakeProvider):
        def list_tables(self, **kwargs):
            scopes.append(current_cancellation_scope())
            entered.set()
            assert release.wait(2)
            message = "obsolete failure"
            raise ConnectionError(message)

    coordinator = CompletionCoordinator("gp", "gp", provider=Provider())
    request = CompletionRequest("gp", "gp", "table", "sample")
    try:
        coordinator.enqueue(request, on_error=lambda *args: errors.append(args))
        assert entered.wait(2)
        if superseded == "generation":
            coordinator.invalidate_tables(rediscover=False)
        else:
            scopes[0].request_cancel()
        release.set()
        _wait_for(lambda: not coordinator.snapshot()[2])
        assert errors == []
        assert coordinator.cached(request) is None
    finally:
        release.set()
        coordinator.stop()
        _wait_for(lambda: coordinator.is_stopped)


def test_cancellation_between_fetch_and_publication_cannot_fill_shared_cache(monkeypatch):
    entered, release = Event(), Event()
    original = completion.CompletionCoordinator._publish_result

    def gated(self, task, values, generation):
        entered.set()
        assert release.wait(2)
        return original(self, task, values, generation)

    monkeypatch.setattr(completion.CompletionCoordinator, "_publish_result", gated)
    coordinator = CompletionCoordinator("gp", "gp", provider=FakeProvider())
    request = CompletionRequest("gp", "gp", "table", "sample")
    try:
        coordinator.enqueue(request, owner_id="owner")
        assert entered.wait(2)
        coordinator.remove_owner("owner")
        release.set()
        _wait_for(lambda: not coordinator.snapshot()[2])
        assert coordinator.cached(request) is None
    finally:
        release.set()
        coordinator.stop()
        _wait_for(lambda: coordinator.is_stopped)


def test_live_clickhouse_completion_keeps_default_database_isolated_from_explicit_prefix(
    monkeypatch,
):
    current = sqlglot.parse_one("SELECT currentDatabase()", read="clickhouse").expressions[0]
    observed = []

    def read(key, statement, **kwargs):
        query = sqlglot.parse_one(statement, read="clickhouse")
        database = next(
            node.expression
            for node in query.find_all(sqlglot.exp.EQ)
            if isinstance(node.this, sqlglot.exp.Column) and node.this.name == "database"
        )
        observed.append(database)
        return pd.DataFrame({"name": ["local_only" if database == current else "remote_only"]})

    monkeypatch.setattr(completion.sql, "read", read)
    provider = completion.ClickHouseCompletionProvider()
    assert provider.list_tables(connection_key="ch", prefix="") == ("local_only",)
    assert provider.list_tables(connection_key="ch", prefix="", schema="system") == ("remote_only",)
    assert observed[0] == current
    assert observed[1].this == "system"


def test_retry_registration_failure_cannot_certify_an_old_cached_namespace(tmp_path, monkeypatch):
    class Provider(FakeProvider):
        schema_reads = 0

        def list_tables(self, **kwargs):
            return (
                MetadataListing((), complete=False)
                if kwargs.get("schema") == "unknown"
                else ("saved",)
            )

        def list_schemas(self, **kwargs):
            self.schema_reads += 1
            return ("sales",)

    store = MetadataStore(tmp_path, "gp")
    store.save({("schema", "", ""): ("sales",), ("table", "", "sales"): ("saved",)})
    provider, storage_errors, busy = Provider(), [], [True]
    discovery = BackgroundMetadata("gp", "gp", provider, store, on_error=storage_errors.append)
    persist = discovery.ledger.request_retry

    def request_retry(keys):
        if busy[0]:
            message = "retry registration busy"
            raise sqlite3.OperationalError(message)
        return persist(keys)

    monkeypatch.setattr(discovery.ledger, "request_retry", request_retry)
    coordinator = CompletionCoordinator("gp", "gp", provider=provider, discovery=discovery)
    ready = Event()
    request = CompletionRequest("gp", "gp", "table", "", schema="unknown")
    try:
        coordinator.enqueue(request, on_error=lambda *args: ready.set())
        assert ready.wait(2)
        assert coordinator.metadata_incomplete
        discovery._discover(0)
        assert provider.schema_reads > 0 or coordinator.metadata_incomplete
        assert discovery.cached("schema") == ("sales",)
        busy[0] = False
        if provider.schema_reads == 0:
            assert discovery._discover(0)
        assert provider.schema_reads == 1
        assert not coordinator.metadata_incomplete
    finally:
        coordinator.stop()
        _wait_for(lambda: coordinator.is_stopped)


def test_successful_catalog_callback_can_stop_without_bootstrap_thread_failure(monkeypatch):
    errors, results, ready = [], [], Event()
    monkeypatch.setattr(threading, "excepthook", lambda event: errors.append(event.exc_value))
    coordinator = CompletionCoordinator("trino", "trino", provider=FakeProvider())

    def received(result):
        results.append(result.suggestions)
        coordinator.stop()
        ready.set()

    try:
        coordinator.enqueue(
            CompletionRequest("trino", "trino", "catalog", ""),
            on_success=received,
            bootstrap=True,
        )
        assert ready.wait(2)
        _wait_for(lambda: coordinator.is_stopped)
        assert len(results) == 1
        assert set(results[0]) == {"hive", "iceberg"}
        assert errors == []
    finally:
        coordinator.stop()
        _wait_for(lambda: coordinator.is_stopped)
