"""Independent background discovery with persistent namespace snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import fields
from datetime import datetime
from threading import Event, Lock, Thread
from time import time
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.execution.cancellation import (
    AsyncSqlCancelled,
    SqlCancellationScope,
    activate_cancellation_scope,
    cancel_scope_queries,
    raise_if_cancelled,
)

from .journal_workers import cancel_metadata
from .metadata_ledger import MetadataLedger, ScanTicket
from .metadata_store import MetadataStore, SnapshotKey, Snapshots
from .metadata_usage import event_usage

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from .completion import MetadataProvider
    from .journal import QueryJournal

HISTORY_PAGE_SIZE = 100


class BackgroundMetadata:
    def __init__(  # noqa: PLR0913 -- connection, provider and persistence dependencies.
        self,
        connection_key: str,
        backend: str,
        provider: MetadataProvider,
        store: MetadataStore | None,
        *,
        default_catalog: str | None = None,
        journal: QueryJournal | None = None,
        on_error: Callable[[Exception], None],
        context: dict[str, Any] | None = None,
    ) -> None:
        self.connection_key = connection_key
        self.journal = journal
        self.backend = backend
        self.provider = provider
        self.store = store
        self.default_catalog = default_catalog
        self.on_error = on_error
        self.context = context or {"catalog": default_catalog}
        self.on_ddl: Callable[[dict[SnapshotKey, str]], None] | None = None
        self._lock = Lock()
        self._persistence_lock = Lock()
        self._wake = Event()
        self._stopping = False
        self._generation = 0
        self._scope = SqlCancellationScope()
        self._cancellations: list[Thread] = []
        self._snapshots: Snapshots = {}
        if store is not None:
            try:
                self._snapshots = store.load()
            except Exception as exc:  # noqa: BLE001 -- cache is optional.
                self.on_error(exc)
                self.store = None
        try:
            self.ledger = MetadataLedger(self.store)
        except Exception as exc:  # noqa: BLE001 -- cache is optional.
            self.on_error(exc)
            self.store = None
            self.ledger = MetadataLedger(None)
        self._ticket: ScanTicket | None = None
        self._history_sequence = self.ledger.checkpoint()
        self._heartbeat_stop = Event()
        self._heartbeat = Thread(target=self._renew_lease, daemon=True)
        self._thread = Thread(target=self._worker, name="sql-explorer-discovery", daemon=True)

    def start(self) -> None:
        if self.journal:
            self.journal.subscribe(self.connection_key, self._wake.set)
        self._wake.set()
        self._heartbeat.start()
        self._thread.start()

    def cached(
        self, kind: str, catalog: str | None = None, schema: str | None = None
    ) -> tuple[str, ...] | None:
        from .completion import normalize_completion_values  # noqa: PLC0415 -- provider protocol.

        catalog = (catalog or self.default_catalog or "") if kind != "catalog" else ""
        with self._lock:
            if kind != "table" or schema is not None:
                return self._snapshots.get((kind, catalog, schema or ""))
            schemas = self._snapshots.get(("schema", catalog, ""))
            if schemas is None:
                return None
            tables = [self._snapshots.get(("table", catalog, name)) for name in schemas]
            if any(values is None for values in tables):
                return None
            return normalize_completion_values(
                name for values in tables if values for name in values
            )

    def invalidate(self) -> None:
        """Wake journal-driven discovery without discarding completed snapshots."""
        self._wake.set()

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            self._cancel()
        self._wake.set()
        self._heartbeat_stop.set()
        if self.journal:
            self.journal.unsubscribe(self.connection_key, self._wake.set)
        if self._thread.ident is None:
            self.ledger.close()

    @property
    def is_stopped(self) -> bool:
        return (
            not self._thread.is_alive()
            and not self._heartbeat.is_alive()
            and not any(t.is_alive() for t in self._cancellations)
        )

    def _cancel(self) -> None:
        self._scope.request_cancel()
        thread = Thread(
            target=cancel_metadata,
            args=(
                self._scope,
                self.journal,
                self.connection_key,
                self.backend,
                cancel_scope_queries,
            ),
            daemon=True,
        )
        self._cancellations = [t for t in self._cancellations if t.is_alive()]
        self._cancellations.append(thread)
        thread.start()

    def _save(self, key: SnapshotKey | None = None) -> Exception | None:
        if self.store is not None:
            try:
                if key is None:
                    self.store.save(self._snapshots)
                else:
                    self.store.update(key, self._snapshots[key])
            except Exception as exc:  # noqa: BLE001 -- discovery continues without disk.
                return exc
        return None

    def _publish(
        self, key: SnapshotKey, values: tuple[str, ...], generation: int, *, persist: bool = True
    ) -> None:
        with self._persistence_lock:
            with self._lock:
                if self._stopping or generation != self._generation:
                    return
                self._snapshots[key] = values
                kind, catalog, _schema = key
                if kind == "catalog":
                    self._snapshots = {
                        k: v
                        for k, v in self._snapshots.items()
                        if k[0] == "catalog" or k[1] in values
                    }
                elif kind == "schema":
                    self._snapshots = {
                        k: v
                        for k, v in self._snapshots.items()
                        if k[0] != "table" or k[1] != catalog or k[2] in values
                    }
            error = self._save(key) if persist else None
        if error is not None:
            self.on_error(error)

    def _seed(self) -> None:
        keys: list[SnapshotKey] = (
            [("catalog", "", "")] if self.backend == "trino" else [("schema", "", "")]
        )
        with self._lock:
            for (kind, catalog, _schema), names in self._snapshots.items():
                if kind == "catalog":
                    keys.extend(("schema", name, "") for name in names)
                elif kind == "schema":
                    keys.extend(("table", catalog, name) for name in names)
        self.ledger.ensure(keys)

    def _consume_history(self) -> None:
        try:
            self._read_history()
        except (OSError, sqlite3.Error, ValueError) as exc:
            if self.journal:
                self.journal.warn(exc)

    def _read_history(self) -> None:
        if self.journal is None:
            return
        store = self.journal.store(self.connection_key)
        while not self._stopping:
            events = store.completed_since(self._history_sequence, HISTORY_PAGE_SIZE)
            for event in events:
                with self._lock:
                    snapshots = dict(self._snapshots)
                identity = event["context"].get("metadata", {}).get("identity")
                scopes, objects = (
                    ({}, [])
                    if (
                        event["backend"] != self.backend
                        or (identity and identity != self.context.get("identity"))
                    )
                    else event_usage(event, snapshots, self.context, self._resolve)
                )
                used_at = datetime.fromisoformat(
                    event["finished_at"].replace("Z", "+00:00")
                ).timestamp()
                self.ledger.consume(
                    event["sequence"],
                    used_at,
                    scopes,
                    objects,
                    historical=bool(event["historical"]),
                )
                self._history_sequence = event["sequence"]
                if self.on_ddl and any(reason == "ddl" for reason in scopes.values()):
                    invalidations = dict(scopes)
                    for kind, catalog, schema, _name in objects:
                        if kind in {"schema", "database"}:
                            invalidations[("table", catalog, schema)] = "ddl"
                    self.on_ddl(invalidations)
            if len(events) < HISTORY_PAGE_SIZE:
                return

    def _resolve(self, name: str, context: dict[str, Any]) -> tuple[str, str] | None:
        resolver = getattr(self.provider, "resolve_reference", None)
        try:
            result: tuple[str, str] | None = (
                resolver(
                    self.connection_key,
                    name,
                    search_path=context.get("search_path"),
                    candidates=context.get("candidates"),
                )
                if resolver
                else None
            )
        except AsyncSqlCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 -- unresolved names must not block discovery.
            self.on_error(exc)
            return None
        else:
            return result

    def _renew_lease(self) -> None:
        while not self._heartbeat_stop.wait(30):
            ticket = self._ticket
            if ticket:
                try:
                    self.ledger.renew(ticket, time())
                except Exception as exc:  # noqa: BLE001 -- optional storage.
                    self.on_error(exc)

    def _discover(self, generation: int) -> bool:
        from .completion import normalize_completion_values  # noqa: PLC0415 -- provider protocol.

        if self.store is not None:
            with self._lock:
                self._snapshots = self.store.load()
        self._consume_history()
        self._seed()
        ticket = self.ledger.claim(time())
        if ticket is None:
            return False
        self._ticket = ticket
        kind, catalog, schema = ticket.key
        try:
            if kind == "catalog":
                result = self.provider.list_catalogs(connection_key=self.connection_key)
            elif kind == "schema":
                result = self.provider.list_schemas(
                    connection_key=self.connection_key, catalog=catalog or None
                )
            else:
                result = self.provider.list_tables(
                    connection_key=self.connection_key,
                    catalog=catalog or None,
                    schema=schema,
                    prefix="",
                )
            raise_if_cancelled()
            self._consume_history()
            names = normalize_completion_values(result)
            if self.ledger.finish(ticket, names, time()):
                self._publish(ticket.key, names, generation, persist=False)
        except AsyncSqlCancelled:
            self.ledger.failed(ticket, time(), cancelled=True)
            raise
        except Exception as exc:
            if self._scope.cancelled:
                self.ledger.failed(ticket, time(), cancelled=True)
                message = "Metadata discovery cancelled"
                raise AsyncSqlCancelled(message) from exc
            self.ledger.failed(ticket, time())
            self.on_error(exc)
        finally:
            self._ticket = None
        return True

    def _worker(self) -> None:
        try:
            self._run_worker()
        finally:
            self._heartbeat_stop.set()
            self._heartbeat.join()
            self.ledger.close()

    def _run_worker(self) -> None:
        while True:
            self._wake.wait(5)
            with self._lock:
                if self._stopping:
                    return
                self._wake.clear()
                generation = self._generation
                self._scope = SqlCancellationScope()
            try:
                with activate_cancellation_scope(self._scope):
                    while not self._stopping and self._discover(generation):
                        pass
            except AsyncSqlCancelled:
                continue
            except Exception as exc:  # noqa: BLE001 -- report failed catalog discovery.
                if not self._scope.cancelled:
                    self.on_error(exc)
                    if self.store and isinstance(exc, (OSError, sqlite3.Error, ValueError)):
                        self.store = None
                        self.ledger = MetadataLedger(None)
                        self._history_sequence = 0


def persistent_discovery(  # noqa: PLR0913 -- provider, cache and journal dependencies.
    directory: Path,
    connection_key: str,
    backend: str,
    provider: MetadataProvider,
    on_error: Callable[[Exception], None],
    *,
    journal: QueryJournal | None = None,
) -> BackgroundMetadata:
    """Bind snapshots to resolved configuration without writing credentials."""
    config = get_connection_config(connection_key)
    values = {field.name: getattr(config, field.name) for field in fields(config)}
    identity = hashlib.sha256(json.dumps(values, sort_keys=True, default=repr).encode()).hexdigest()
    return BackgroundMetadata(
        connection_key,
        backend,
        provider,
        MetadataStore(directory, identity),
        default_catalog=getattr(config, "catalog", None),
        on_error=on_error,
        journal=journal,
        context={
            "identity": identity,
            "catalog": getattr(config, "catalog", None),
            "schema": getattr(config, "schema", None),
            "database": getattr(config, "database", None),
        },
    )
