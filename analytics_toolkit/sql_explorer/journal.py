"""Private, per-action query journal backed by SQLite."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from analytics_toolkit.sql.execution.cancellation import AsyncSqlCancelled
from analytics_toolkit.sql.execution.observation import observe_sql

from .journal_metadata import describe_sql
from .journal_store import USER_ORIGINS, JournalStore

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


def safe_component(value: str) -> str:
    safe = "".join(c if c.isascii() and (c.isalnum() or c in "-_") else "_" for c in value)
    safe = safe[:80] or "unnamed"
    # Hash every alias so case-insensitive filesystems and escaped names cannot
    # merge the histories of distinct connection aliases.
    return safe + "_" + hashlib.sha256(value.encode()).hexdigest()[:16]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class QueryJournal:
    def __init__(self, directory: Path) -> None:
        self.directory = directory / "query_journal"
        self._lock = RLock()
        self._warning: str | None = None
        self._warned = False
        self._stores: dict[str, JournalStore] = {}
        self._listeners: dict[str, set[Callable[[], None]]] = {}

    def subscribe(self, alias: str, callback: Callable[[], None]) -> None:
        with self._lock:
            self._listeners.setdefault(alias.casefold(), set()).add(callback)

    def unsubscribe(self, alias: str, callback: Callable[[], None]) -> None:
        with self._lock:
            self._listeners.get(alias.casefold(), set()).discard(callback)

    def completed(self, alias: str) -> None:
        with self._lock:
            callbacks = tuple(self._listeners.get(alias.casefold(), ()))
        for callback in callbacks:
            self._notify(callback)

    def _notify(self, callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception as exc:  # noqa: BLE001 -- optional observers cannot break SQL.
            self.warn(exc)

    def alias_directory(self, alias: str) -> Path:
        return self.directory / safe_component(alias)

    def store(self, alias: str) -> JournalStore:
        with self._lock:
            if alias not in self._stores:
                self._stores[alias] = JournalStore(self.alias_directory(alias) / "journal.sqlite3")
            return self._stores[alias]

    def warn(self, error: Exception) -> None:
        with self._lock:
            if not self._warned:
                self._warning = f"Query journal unavailable: {error}"

    def take_warning(self) -> str | None:
        with self._lock:
            warning, self._warning = self._warning, None
            self._warned = self._warned or warning is not None
            return warning

    @contextmanager
    def action(  # noqa: PLR0913 -- immutable execution context and provenance.
        self,
        alias: str,
        backend: str,
        origin: str,
        *,
        user_sql: str | None = None,
        source_file: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> Iterator[JournalAction]:
        action = JournalAction(self, alias, backend, origin, user_sql, source_file, context)
        with ExitStack() as stack:
            try:
                stack.enter_context(action.store.session())
            except (OSError, sqlite3.Error, ValueError) as exc:
                action.disable(exc)
            action.start()
            try:
                with observe_sql(action):
                    yield action
            except BaseException as exc:
                action.finish("cancelled" if isinstance(exc, AsyncSqlCancelled) else "failed", exc)
                raise
            else:
                action.finish("completed")


class JournalAction:
    def __init__(  # noqa: PLR0913 -- immutable action snapshot.
        self,
        journal: QueryJournal,
        alias: str,
        backend: str,
        origin: str,
        user_sql: str | None,
        source_file: str | None,
        context: dict[str, Any] | None,
    ) -> None:
        self.journal = journal
        self._lock = RLock()
        self._started = monotonic()
        identity = uuid4().hex
        self.store = journal.store(alias)
        self._submission_count = 0
        self._disabled = False
        self.record: dict[str, Any] = {
            "schema_version": 1,
            "action_id": identity,
            "connection_alias": alias,
            "backend": backend,
            "origin": origin,
            "source_file": source_file,
            "user_sql": user_sql,
            "context": context or {},
            "started_at": utc_now(),
            "finished_at": None,
            "elapsed_seconds": None,
            "outcome": "running",
            "error": None,
            "statements": describe_sql(user_sql or "", backend),
        }

    def _save(self, callback: Callable[..., None], *args: Any) -> None:
        if self._disabled:
            return
        try:
            callback(*args)
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            self.disable(exc)

    def disable(self, error: Exception) -> None:
        self._disabled = True
        self.journal.warn(error)

    def start(self) -> None:
        self._save(self.store.start_action, self.record)

    def finish(self, outcome: str, error: BaseException | None = None) -> None:
        with self._lock:
            if self.record.pop("cancelled", False):
                outcome = "cancelled"
            self.record.update(
                outcome=outcome,
                finished_at=utc_now(),
                elapsed_seconds=max(0.0, monotonic() - self._started),
                error={"type": type(error).__name__, "message": str(error)} if error else None,
            )
            self._save(self.store.finish_action, self.record)
            if outcome == "completed" and self.record["origin"] in USER_ORIGINS:
                self.journal.completed(self.record["connection_alias"])

    @contextmanager
    def submission(self, sql: str) -> Iterator[None]:
        started = monotonic()
        with self._lock:
            item = {
                "index": self._submission_count,
                "sql": sql,
                "started_at": utc_now(),
                "finished_at": None,
                "elapsed_seconds": None,
                "outcome": "running",
                "error": None,
                "statements": describe_sql(sql, self.record["backend"]),
            }
            self._submission_count += 1
            self._save(self.store.start_submission, self.record["action_id"], item)
        try:
            yield
        except BaseException as exc:
            item["outcome"] = "cancelled" if isinstance(exc, AsyncSqlCancelled) else "failed"
            item["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        else:
            item["outcome"] = "completed"
        finally:
            with self._lock:
                item.update(finished_at=utc_now(), elapsed_seconds=max(0.0, monotonic() - started))
                self._save(self.store.finish_submission, self.record["action_id"], item)
