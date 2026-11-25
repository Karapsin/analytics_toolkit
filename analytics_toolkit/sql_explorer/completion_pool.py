"""Live-owner subscriptions and configuration replacement for shared metadata workers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from .completion import CompletionCoordinator, CompletionResult
    from .journal import QueryJournal


@dataclass
class _Owner:
    on_error: Callable[[CompletionResult, Exception], None] | None = None
    on_complete: Callable[[], None] | None = None
    on_replace: Callable[[CompletionCoordinator], None] | None = None
    on_state: (
        Callable[[CompletionCoordinator, CompletionResult | None, Exception | None], None] | None
    ) = None


@dataclass
class _CoordinatorPoolEntry:
    coordinator: CompletionCoordinator
    owners: dict[str, _Owner] = field(default_factory=dict)
    configuration_identity: str | None = None


def _api() -> Any:
    from . import completion  # noqa: PLC0415 -- preserve the established injectable facade.

    return completion


class CompletionCoordinatorPool:
    """Share workers while keeping callbacks attached to every current owner."""

    def __init__(
        self, *, state_directory: Path | None = None, journal: QueryJournal | None = None
    ) -> None:
        self._state_directory = state_directory
        self.journal = journal
        self._entries: dict[str, _CoordinatorPoolEntry] = {}
        self._retired: list[CompletionCoordinator] = []

    def acquire(  # noqa: PLR0913 -- callbacks belong to this individual owner.
        self,
        connection_key: str,
        backend: str,
        owner_id: str,
        *,
        on_error: Callable[[CompletionResult, Exception], None] | None = None,
        on_complete: Callable[[], None] | None = None,
        on_replace: Callable[[CompletionCoordinator], None] | None = None,
        on_state: Callable[[CompletionCoordinator, CompletionResult | None, Exception | None], None]
        | None = None,
    ) -> CompletionCoordinator:
        api = _api()
        key = connection_key.casefold()
        entry = self._entries.get(key)
        identity = api.journal_context(connection_key).get("identity")
        previous: dict[str, _Owner] = {}
        if entry is not None and identity is not None and entry.configuration_identity != identity:
            previous = dict(entry.owners)
            entry.coordinator.stop()
            self._retired.append(entry.coordinator)
            entry = None
        owner = _Owner(on_error, on_complete, on_replace, on_state)
        if entry is None:
            provider = api.provider_for_backend(backend)
            if self.journal is not None:
                provider = api.JournalMetadataProvider(provider, self.journal, backend, "metadata")
            coordinator = api.CompletionCoordinator(
                connection_key, backend, provider=provider, journal=self.journal
            )
            entry = _CoordinatorPoolEntry(coordinator, previous, identity)
            entry.owners[owner_id] = owner
            self._entries[key] = entry
            for callback in tuple(previous.values()):
                if callback.on_replace is not None:
                    callback.on_replace(coordinator)
            self._start(key, entry)
        else:
            entry.owners[owner_id] = owner
        return entry.coordinator

    def _start(self, key: str, entry: _CoordinatorPoolEntry) -> None:
        api = _api()
        coordinator = entry.coordinator

        def report_result(result: CompletionResult, exc: Exception) -> None:
            self._notify(key, entry, result, exc)

        if self._state_directory is None:
            coordinator.start_bootstrap(on_error=report_result)
            return

        def report(exc: Exception) -> None:
            report_result(
                api.CompletionResult(
                    api.CompletionRequest(
                        coordinator.connection_key, coordinator.backend, "catalog", ""
                    ),
                    0,
                    (),
                ),
                exc,
            )

        try:
            provider = api.provider_for_backend(coordinator.backend)
            if self.journal is not None:
                provider = api.JournalMetadataProvider(
                    provider, self.journal, coordinator.backend, "background"
                )
            coordinator.discovery = api.persistent_discovery(
                self._state_directory,
                coordinator.connection_key,
                coordinator.backend,
                provider,
                report,
                journal=self.journal,
                on_complete=lambda: self._notify(key, entry),
            )
            coordinator.bind_discovery()
            coordinator.discovery.on_ddl = coordinator.invalidate_scopes
            if coordinator.discovery.incomplete:
                report(
                    api.IncompleteMetadataError(
                        "Metadata is incomplete; retaining the last complete names."
                    )
                )
            coordinator.discovery.start()
        except Exception as exc:  # noqa: BLE001 -- interactive discovery remains available.
            report(exc)

    def _notify(
        self,
        key: str,
        entry: _CoordinatorPoolEntry,
        result: CompletionResult | None = None,
        exc: Exception | None = None,
    ) -> None:
        if self._entries.get(key) is not entry:
            return
        for owner_id, owner in tuple(entry.owners.items()):
            if self._entries.get(key) is not entry or entry.owners.get(owner_id) is not owner:
                continue
            if owner.on_state is not None:
                owner.on_state(entry.coordinator, result, exc)
            elif exc is not None and owner.on_error is not None and result is not None:
                owner.on_error(result, exc)
            elif exc is None and owner.on_complete is not None:
                owner.on_complete()

    def release(self, owner_id: str, connection_key: str | None = None) -> None:
        keys = (connection_key.casefold(),) if connection_key is not None else tuple(self._entries)
        for key in keys:
            entry = self._entries.get(key)
            if entry is None or owner_id not in entry.owners:
                continue
            entry.coordinator.remove_owner(owner_id)
            entry.owners.pop(owner_id)
            if not entry.owners:
                entry.coordinator.stop()
                self._retired.append(entry.coordinator)
                self._entries.pop(key)

    def stop(self) -> None:
        for entry in self._entries.values():
            entry.coordinator.stop()
            self._retired.append(entry.coordinator)
        self._entries.clear()

    @property
    def is_stopped(self) -> bool:
        self._retired = [item for item in self._retired if not item.is_stopped]
        return not self._entries and not self._retired

    def coordinator_for(self, connection_key: str) -> CompletionCoordinator | None:
        entry = self._entries.get(connection_key.casefold())
        return entry.coordinator if entry is not None else None

    def snapshot(self) -> dict[str, tuple[int, int, bool]]:
        return {key: entry.coordinator.snapshot() for key, entry in self._entries.items()}
