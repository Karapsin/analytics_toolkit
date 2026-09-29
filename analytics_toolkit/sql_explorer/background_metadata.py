"""Independent background discovery with persistent namespace snapshots."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING

from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.execution.cancellation import (
    AsyncSqlCancelled,
    SqlCancellationScope,
    activate_cancellation_scope,
    cancel_scope_queries,
    raise_if_cancelled,
)

from .metadata_store import MetadataStore, SnapshotKey, Snapshots

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from .completion import MetadataProvider


class BackgroundMetadata:
    def __init__(  # noqa: PLR0913 -- connection, provider and persistence dependencies.
        self,
        connection_key: str,
        backend: str,
        provider: MetadataProvider,
        store: MetadataStore | None,
        *,
        default_catalog: str | None = None,
        on_error: Callable[[Exception], None],
    ) -> None:
        self.connection_key = connection_key
        self.backend = backend
        self.provider = provider
        self.store = store
        self.default_catalog = default_catalog
        self.on_error = on_error
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
        self._thread = Thread(target=self._worker, name="sql-explorer-discovery", daemon=True)

    def start(self) -> None:
        self._wake.set()
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
        with self._persistence_lock:
            with self._lock:
                self._generation += 1
                self._snapshots.clear()
                self._cancel()
            error = self._save()
        if error is not None:
            self.on_error(error)
        self._wake.set()

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            self._cancel()
        self._wake.set()

    @property
    def is_stopped(self) -> bool:
        return not self._thread.is_alive() and not any(t.is_alive() for t in self._cancellations)

    def _cancel(self) -> None:
        self._scope.request_cancel()
        thread = Thread(target=cancel_scope_queries, args=(self._scope,), daemon=True)
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

    def _publish(self, key: SnapshotKey, values: tuple[str, ...], generation: int) -> None:
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
            error = self._save(key)
        if error is not None:
            self.on_error(error)

    def _discover(self, generation: int) -> None:
        from .completion import normalize_completion_values  # noqa: PLC0415 -- provider protocol.

        catalogs: tuple[str | None, ...] = (None,)
        if self.backend == "trino":
            names = normalize_completion_values(
                self.provider.list_catalogs(connection_key=self.connection_key)
            )
            raise_if_cancelled()
            self._publish(("catalog", "", ""), names, generation)
            catalogs = names
        for catalog in catalogs:
            raise_if_cancelled()
            try:
                schemas = normalize_completion_values(
                    self.provider.list_schemas(connection_key=self.connection_key, catalog=catalog)
                )
                raise_if_cancelled()
                self._publish(("schema", catalog or "", ""), schemas, generation)
            except AsyncSqlCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 -- another catalog can still succeed.
                raise_if_cancelled()
                self.on_error(exc)
                continue
            for schema in schemas:
                raise_if_cancelled()
                try:
                    names = normalize_completion_values(
                        self.provider.list_tables(
                            connection_key=self.connection_key,
                            catalog=catalog,
                            schema=schema,
                            prefix="",
                        )
                    )
                    raise_if_cancelled()
                    self._publish(("table", catalog or "", schema), names, generation)
                except AsyncSqlCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 -- retain the previous schema snapshot.
                    raise_if_cancelled()
                    self.on_error(exc)

    def _worker(self) -> None:
        while True:
            self._wake.wait()
            with self._lock:
                if self._stopping:
                    return
                self._wake.clear()
                generation = self._generation
                self._scope = SqlCancellationScope()
            try:
                with activate_cancellation_scope(self._scope):
                    self._discover(generation)
            except AsyncSqlCancelled:
                continue
            except Exception as exc:  # noqa: BLE001 -- report failed catalog discovery.
                if not self._scope.cancelled:
                    self.on_error(exc)


def persistent_discovery(
    directory: Path,
    connection_key: str,
    backend: str,
    provider: MetadataProvider,
    on_error: Callable[[Exception], None],
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
    )
