from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from analytics_toolkit.sql_explorer.background_metadata import BackgroundMetadata
from analytics_toolkit.sql_explorer.journal import QueryJournal
from analytics_toolkit.sql_explorer.journal_metadata import describe_sql
from analytics_toolkit.sql_explorer.metadata_ledger import (
    LEASE_SECONDS,
    REFRESH_SECONDS,
    MetadataLedger,
)
from analytics_toolkit.sql_explorer.metadata_store import MetadataStore
from analytics_toolkit.sql_explorer.metadata_usage import event_usage

from tests.sql.explorer.completion import FakeProvider, _wait_for

if TYPE_CHECKING:
    from pathlib import Path

TABLE = ("table", "", "sales")


def test_usage_waits_24_hours_and_is_consumed_once(tmp_path: Path) -> None:
    ledger = MetadataLedger(MetadataStore(tmp_path, "gp"))
    ledger.ensure([TABLE])
    ticket = ledger.claim(100)
    assert ticket is not None
    assert ledger.finish(ticket, (), 100)
    assert ledger.consume(1, 101, {TABLE: ""}, [("table", "", "sales", "orders")])
    assert not ledger.consume(1, 101, {TABLE: "ddl"}, [])
    assert ledger.claim(100 + REFRESH_SECONDS - 1) is None
    ticket = ledger.claim(100 + REFRESH_SECONDS)
    assert ticket is not None
    assert ledger.finish(ticket, ("orders",), 100 + REFRESH_SECONDS)
    assert ledger.claim(100 + 2 * REFRESH_SECONDS) is None
    reopened = MetadataLedger(MetadataStore(tmp_path, "gp"))
    assert reopened.checkpoint() == 1
    assert reopened.claim(100 + 3 * REFRESH_SECONDS) is None


def test_legacy_snapshot_timestamps_and_empty_snapshots_are_preserved(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path, "gp")
    store.save({TABLE: ()})
    ledger = MetadataLedger(store)
    ledger.ensure([TABLE])
    assert ledger.claim(10**12) is None
    ledger.consume(1, 1, {TABLE: "missing"}, [], historical=True)
    assert ledger.claim(10**12) is None
    assert store.load()[TABLE] == ()


def test_late_committed_event_is_not_hidden_by_earlier_finish_timestamp(tmp_path: Path) -> None:
    ledger = MetadataLedger(MetadataStore(tmp_path, "gp"))
    ledger.ensure([TABLE])
    ticket = ledger.claim(100)
    assert ticket is not None
    assert ledger.finish(ticket, ("orders",), 110)
    # Journal commit waited for another writer, after its finished_at was captured.
    ledger.consume(1, 105, {TABLE: "missing"}, [])
    assert ledger.claim(111) is not None


def test_urgent_work_precedes_initial_scan_and_supersedes_running_result(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path, "gp")
    ledger = MetadataLedger(store)
    ledger.ensure([TABLE, ("table", "", "unused")])
    ledger.consume(1, 101, {TABLE: "missing"}, [])
    ticket = ledger.claim(102)
    assert ticket is not None
    assert ticket.key == TABLE
    ledger.consume(2, 103, {TABLE: "ddl"}, [])
    assert not ledger.finish(ticket, ("stale",), 104)
    assert TABLE not in store.load()
    replacement = ledger.claim(105)
    assert replacement is not None
    assert replacement.key == TABLE
    assert ledger.finish(replacement, ("fresh",), 106)
    assert store.load()[TABLE] == ("fresh",)


def test_query_arriving_during_scan_is_not_lost(tmp_path: Path) -> None:
    ledger = MetadataLedger(MetadataStore(tmp_path, "gp"))
    ledger.ensure([TABLE])
    ticket = ledger.claim(100)
    assert ticket is not None
    ledger.consume(1, 101, {TABLE: ""}, [])
    assert ledger.finish(ticket, ("orders",), 102)
    assert ledger.claim(102 + REFRESH_SECONDS - 1) is None
    assert ledger.claim(102 + REFRESH_SECONDS) is not None


def test_concurrent_claims_renewal_crash_recovery_and_stale_owner(tmp_path: Path) -> None:
    first = MetadataLedger(MetadataStore(tmp_path, "gp"))
    second = MetadataLedger(MetadataStore(tmp_path, "gp"))
    first.ensure([TABLE])
    old = first.claim(100)
    assert old is not None
    assert second.claim(101) is None
    first.renew(old, 200)
    assert second.claim(100 + LEASE_SECONDS) is None
    new = second.claim(200 + LEASE_SECONDS)
    assert new is not None
    assert not first.finish(old, ("old",), 600)
    assert second.finish(new, ("new",), 601)


def test_failure_backoff_and_cancellation_never_complete_scan(tmp_path: Path) -> None:
    ledger = MetadataLedger(MetadataStore(tmp_path, "gp"))
    ledger.ensure([TABLE])
    ticket = ledger.claim(100)
    assert ticket is not None
    ledger.failed(ticket, 101)
    assert ledger.claim(160) is None
    retry = ledger.claim(161)
    assert retry is not None
    ledger.failed(retry, 162, cancelled=True)
    assert ledger.claim(162) is not None


def test_only_used_schema_is_refreshed_under_used_catalog() -> None:
    snapshots = {
        ("catalog", "", ""): ("lake",),
        ("schema", "lake", ""): ("sales", "unused"),
        ("table", "lake", "sales"): ("orders",),
        ("table", "lake", "unused"): ("other",),
    }
    event = {
        "backend": "trino",
        "context": {},
        "statements": describe_sql("select * from lake.sales.orders", "trino"),
    }
    scopes, objects = event_usage(event, snapshots, {}, lambda name, context: None)
    assert scopes == {
        ("catalog", "", ""): "",
        ("schema", "lake", ""): "",
        ("table", "lake", "sales"): "",
    }
    assert objects == [("table", "lake", "sales", "orders")]


def test_unknown_object_and_ddl_only_queue_affected_namespace() -> None:
    snapshots = {("schema", "", ""): ("sales",), TABLE: ("orders",)}
    event = {
        "backend": "gp",
        "context": {},
        "statements": describe_sql("create table sales.copy as select * from sales.orders", "gp"),
    }
    scopes, _ = event_usage(event, snapshots, {}, lambda name, context: None)
    assert scopes[TABLE] == "ddl"
    assert scopes[("schema", "", "")] == ""
    event["statements"] = describe_sql("select * from new_schema.new_table", "gp")
    scopes, _ = event_usage(event, snapshots, {}, lambda name, context: None)
    assert scopes[("schema", "", "")] == "missing"
    assert scopes[("table", "", "new_schema")] == "missing"


def test_legacy_ambiguous_names_are_not_guessed_and_defaults_resolve_new_actions() -> None:
    snapshots = {TABLE: ("orders",), ("table", "", "other"): ("orders",)}
    event = {
        "backend": "ch",
        "context": {},
        "statements": describe_sql("select * from orders", "ch"),
    }
    assert event_usage(event, snapshots, {}, lambda name, context: None) == ({}, [])
    event["context"] = {"metadata": {"database": "sales"}}
    scopes, _ = event_usage(event, snapshots, {}, lambda name, context: None)
    assert TABLE in scopes


def test_scan_once_across_relaunch_and_successful_query_wakes_missing_scan(tmp_path: Path) -> None:
    class Provider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.scans = []
            self.names = ("orders",)

        def list_schemas(self, **kwargs):
            self.scans.append("schemas")
            return ("sales",)

        def list_tables(self, **kwargs):
            self.scans.append(kwargs["schema"])
            return self.names

    provider = Provider()
    journal = QueryJournal(tmp_path)
    store = MetadataStore(tmp_path, "gp")
    errors = []
    first = BackgroundMetadata("gp", "gp", provider, store, journal=journal, on_error=errors.append)
    first.start()
    _wait_for(lambda: first.cached("table", schema="sales") == ("orders",))
    first.stop()
    _wait_for(lambda: first.is_stopped)
    assert provider.scans == ["schemas", "sales"]
    second = BackgroundMetadata(
        "gp", "gp", provider, store, journal=journal, on_error=errors.append
    )
    # A deterministic idle pass performs no database queries.
    assert not second._discover(0)
    assert provider.scans == ["schemas", "sales"]
    second.start()
    try:
        provider.names = ("new_table", "orders")
        with journal.action("gp", "gp", "user", user_sql="select * from sales.new_table"):
            pass
        _wait_for(lambda: second.cached("table", schema="sales") == provider.names)
        assert provider.scans == ["schemas", "sales", "sales"]
        assert not errors
    finally:
        second.stop()
        _wait_for(lambda: second.is_stopped)


def test_history_checkpoint_replay_ignores_other_identity_and_backend(tmp_path: Path) -> None:
    journal = QueryJournal(tmp_path)
    for backend, identity in [("trino", "identity"), ("gp", "different")]:
        with journal.action(
            "gp",
            backend,
            "user",
            user_sql="select * from sales.orders",
            context={"metadata": {"identity": identity}},
        ):
            pass
    discovery = BackgroundMetadata(
        "gp",
        "gp",
        FakeProvider(),
        MetadataStore(tmp_path, "identity"),
        journal=journal,
        context={"identity": "identity"},
        on_error=lambda exc: None,
    )
    discovery._consume_history()
    assert discovery.ledger.checkpoint() == 2
    assert discovery.ledger.claim(datetime.now(timezone.utc).timestamp()) is None
