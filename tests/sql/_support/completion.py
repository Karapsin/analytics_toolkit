"""Metadata fakes and bounded waits shared by Explorer tests."""

from __future__ import annotations

from threading import Lock
from time import monotonic, sleep
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from analytics_toolkit.sql_explorer.app import SqlExplorerApp
    from analytics_toolkit.sql_explorer.completion import CompletionRequest


class FakeProvider:
    def __init__(self) -> None:
        self.table_calls: list[tuple[str, str | None, str | None]] = []
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.lock = Lock()

    def _enter(self, value: str) -> None:
        with self.lock:
            self.calls.append(value)
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def _leave(self) -> None:
        with self.lock:
            self.active -= 1

    def list_tables(
        self,
        *,
        connection_key: str,
        prefix: str,
        schema: str | None = None,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        assert connection_key
        self._enter(f"table:{prefix}")
        self.table_calls.append((prefix, schema, catalog))
        self._leave()
        return (f"{prefix}_one", f"{prefix}_two", "unrelated")

    def list_catalogs(self, *, connection_key: str) -> tuple[str, ...]:
        assert connection_key
        self._enter("catalog")
        self._leave()
        return ("iceberg", "hive")

    def list_schemas(
        self,
        *,
        connection_key: str,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        assert connection_key
        self._enter(f"schema:{catalog}")
        self._leave()
        return ("analytics", "sandbox")


class StubCoordinator:
    def __init__(self) -> None:
        self.catalogs: tuple[str, ...] | None = ()
        self.schemas: dict[str | None, tuple[str, ...] | None] = {None: ()}
        self.table_cache: tuple[str, ...] | None = None
        self.schema_enqueues = 0

    def stop(self) -> None:
        return None

    def remove_owner(self, owner_id: str) -> None:
        del owner_id

    def known_catalogs(self) -> tuple[str, ...] | None:
        return self.catalogs

    def cached_schemas(self, catalog: str | None = None) -> tuple[str, ...] | None:
        return self.schemas.get(catalog)

    def cached(self, _request: CompletionRequest) -> tuple[str, ...] | None:
        return self.table_cache

    def enqueue_schemas(self, **_kwargs: object) -> int:
        self.schema_enqueues += 1
        return self.schema_enqueues

    def enqueue(self, *_args: object, **_kwargs: object) -> int:
        return 1


def _install_stub(application: SqlExplorerApp) -> StubCoordinator:
    assert application._completion is not None
    application._completion.stop()
    stub = StubCoordinator()
    application._completion = stub  # type: ignore[assignment]
    return stub


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.01)
    message = "timed out waiting for metadata worker"
    raise AssertionError(message)
