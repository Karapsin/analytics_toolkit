from __future__ import annotations

import sqlite3
from contextlib import closing
from unittest.mock import Mock

import pytest
from analytics_toolkit.sql.execution.cancellation import AsyncSqlCancelled
from analytics_toolkit.sql_explorer import background_metadata as module
from analytics_toolkit.sql_explorer.background_metadata import BackgroundMetadata
from analytics_toolkit.sql_explorer.completion import CompletionCoordinator, CompletionRequest
from analytics_toolkit.sql_explorer.journal import QueryJournal
from analytics_toolkit.sql_explorer.journal_workers import JournalMetadataProvider
from analytics_toolkit.sql_explorer.metadata_ledger import MetadataLedger
from analytics_toolkit.sql_explorer.metadata_store import MetadataStore

from tests.sql.explorer.completion import FakeProvider, _wait_for


def test_future_metadata_format_falls_back_without_replacing_saved_names(tmp_path):
    store = MetadataStore(tmp_path, "gp")
    store.save({("schema", "", ""): ("sales",)})
    with closing(store.connect()) as connection, connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="Unsupported metadata"):
        MetadataLedger(store)
    errors = []
    discovery = BackgroundMetadata("gp", "gp", FakeProvider(), store, on_error=errors.append)
    try:
        assert discovery.store is None
        assert discovery.cached("schema") == ("sales",)
        assert len(errors) == 1
        with closing(store.connect()) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
    finally:
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)


def test_optional_snapshot_writes_fail_without_losing_live_completion(tmp_path, monkeypatch):
    store = MetadataStore(tmp_path, "gp")
    errors = []
    discovery = BackgroundMetadata("gp", "gp", FakeProvider(), store, on_error=errors.append)
    coordinator = CompletionCoordinator("gp", "gp", provider=FakeProvider(), discovery=discovery)
    try:
        discovery._snapshots = {("schema", "", ""): ("sales",)}
        assert discovery._save() is None
        assert store.load() == discovery._snapshots
        monkeypatch.setattr(store, "update", Mock(side_effect=OSError("read only")))
        discovery._publish(("table", "", "sales"), ("orders",), 0)
        assert str(errors[0]) == "read only"
        assert coordinator.cached(
            CompletionRequest("gp", "gp", "table", "ord", schema="sales")
        ) == ("orders",)
        coordinator.invalidate_tables()
        assert discovery._wake.is_set()
        discovery.store = None
        assert discovery._save() is None
    finally:
        coordinator.stop()
        _wait_for(lambda: coordinator.is_stopped)


@pytest.mark.parametrize("with_journal", [True, False])
def test_history_read_failure_is_optional(tmp_path, monkeypatch, with_journal):
    journal = QueryJournal(tmp_path) if with_journal else None
    discovery = BackgroundMetadata(
        "gp", "gp", FakeProvider(), None, journal=journal, on_error=lambda error: None
    )
    try:
        monkeypatch.setattr(
            discovery, "_read_history", Mock(side_effect=sqlite3.OperationalError("busy"))
        )
        discovery._consume_history()
        if journal:
            assert "busy" in journal.take_warning()
    finally:
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)


def test_history_pages_invalidate_dropped_schema_and_table_columns(tmp_path, monkeypatch):
    journal = QueryJournal(tmp_path)
    for statement in ("DROP TABLE sales.orders", "DROP SCHEMA sales"):
        with journal.action("gp", "gp", "user", user_sql=statement):
            pass
    monkeypatch.setattr(module, "HISTORY_PAGE_SIZE", 1)
    discovery = BackgroundMetadata(
        "gp", "gp", FakeProvider(), None, journal=journal, on_error=lambda error: None
    )
    invalidations = []
    discovery.on_ddl = invalidations.append
    try:
        discovery._snapshots = {("schema", "", ""): ("sales",), ("table", "", "sales"): ("orders",)}
        discovery._read_history()
        assert len(invalidations) == 2
        assert all(item[("table", "", "sales")] == "ddl" for item in invalidations)
        assert discovery._history_sequence == 2
        discovery._stopping = True
        discovery._read_history()
    finally:
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)


def test_reference_resolver_journals_internal_queries_and_isolates_failure(tmp_path):
    journal = QueryJournal(tmp_path)
    provider = FakeProvider()
    wrapper = JournalMetadataProvider(provider, journal, "gp", "background")
    assert wrapper.resolve_reference("gp", "orders") is None
    resolver = Mock(return_value=("", "sales"))
    provider.resolve_reference = resolver
    errors = []
    discovery = BackgroundMetadata("gp", "gp", wrapper, None, on_error=errors.append)
    try:
        assert discovery._resolve(
            "orders", {"search_path": ["sales"], "candidates": [("", "sales")]}
        ) == ("", "sales")
        resolver.assert_called_once_with(
            "gp", "orders", search_path=["sales"], candidates=[("", "sales")]
        )
        assert journal.store("gp").completed_since(0) == []
        resolver.side_effect = RuntimeError("unavailable")
        assert discovery._resolve("orders", {}) is None
        assert str(errors[0]) == "unavailable"
        resolver.side_effect = AsyncSqlCancelled("cancelled")
        with pytest.raises(AsyncSqlCancelled):
            discovery._resolve("orders", {})
        discovery.provider = FakeProvider()
        assert discovery._resolve("orders", {}) is None
    finally:
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)


def test_lease_heartbeat_renews_active_claim_and_reports_storage_failure(monkeypatch):
    errors = []
    discovery = BackgroundMetadata("gp", "gp", FakeProvider(), None, on_error=errors.append)
    discovery.ledger.ensure([("schema", "", "")])
    ticket = discovery.ledger.claim(100)
    waits = iter([False, False, False, True])
    renew = Mock(side_effect=[None, OSError("disk")])
    monkeypatch.setattr(discovery.ledger, "renew", renew)

    def wait(timeout):
        value = next(waits)
        if renew.call_count < 2 and discovery._ticket is None:
            discovery._ticket = ticket
        elif renew.call_count == 2:
            discovery._ticket = None
        return value

    monkeypatch.setattr(discovery._heartbeat_stop, "wait", wait)
    try:
        discovery._renew_lease()
        assert renew.call_count == 2
        assert str(errors[0]) == "disk"
    finally:
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)


@pytest.mark.parametrize(
    ("cancelled", "failure", "fallback"),
    [
        (False, sqlite3.OperationalError, True),
        (True, sqlite3.OperationalError, False),
        (False, RuntimeError, False),
    ],
)
def test_worker_storage_failure_switches_to_memory_unless_cancelled(
    tmp_path, monkeypatch, cancelled, failure, fallback
):
    errors = []
    discovery = BackgroundMetadata(
        "gp", "gp", FakeProvider(), MetadataStore(tmp_path, "gp"), on_error=errors.append
    )
    waits = 0

    def wait(timeout):
        nonlocal waits
        waits += 1
        if waits > 1:
            discovery._stopping = True

    def discover(generation):
        if cancelled:
            discovery._scope.request_cancel()
        message = "disk failed"
        raise failure(message)

    monkeypatch.setattr(discovery._wake, "wait", wait)
    monkeypatch.setattr(discovery, "_discover", discover)
    try:
        discovery._run_worker()
        assert (discovery.store is None) is fallback
        assert len(errors) == (0 if cancelled else 1)
    finally:
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)


def test_late_ddl_during_fetch_rejects_stale_result(monkeypatch):
    discovery = BackgroundMetadata("gp", "gp", FakeProvider(), None, on_error=lambda error: None)
    count = 0

    def history():
        nonlocal count
        count += 1
        if count == 2:
            discovery.ledger.consume(1, 1, {("schema", "", ""): "ddl"}, [])

    monkeypatch.setattr(discovery, "_consume_history", history)
    try:
        assert discovery._discover(0)
        assert discovery.cached("schema") is None
        assert discovery._discover(0)
        assert discovery.cached("schema") is not None
    finally:
        discovery.stop()
        _wait_for(lambda: discovery.is_stopped)


def test_completion_listener_failure_does_not_lose_history(tmp_path):
    journal = QueryJournal(tmp_path)
    journal.subscribe("gp", Mock(side_effect=RuntimeError("observer failed")))
    with journal.action("gp", "gp", "user", user_sql="select 1"):
        pass
    assert "observer failed" in journal.take_warning()
    assert len(journal.store("gp").completed_since(0)) == 1


def test_form_creation_feed_uses_completed_mutations_only(tmp_path):
    journal = QueryJournal(tmp_path)
    with journal.action("gp", "gp", "create_table") as action:
        for statement in ("SELECT * FROM source.orders", "CREATE TABLE sales.orders (id INT)"):
            with action.submission(statement):
                pass
        message = "failed"
        with pytest.raises(ValueError, match="failed"), action.submission("DROP TABLE sales.keep"):
            raise ValueError(message)
    events = journal.store("gp").completed_since(0)
    assert [statement["action"] for statement in events[0]["statements"]] == ["create"]


def test_journal_read_never_creates_missing_file_and_rechecks_version(tmp_path, monkeypatch):
    journal = QueryJournal(tmp_path)
    store = journal.store("gp")
    with pytest.raises(sqlite3.OperationalError), store.connect():
        pass
    assert not store.path.exists()
    with journal.action("gp", "gp", "user", user_sql="select 1"):
        pass
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("PRAGMA user_version=99")
    monkeypatch.setattr(store, "_initialize", lambda: None)
    with pytest.raises(ValueError, match="Unsupported query journal"), store.connect():
        pass
