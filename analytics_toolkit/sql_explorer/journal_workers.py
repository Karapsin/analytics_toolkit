"""Journal scopes for Explorer metadata and cancellation workers."""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.execution.cancellation import cancel_scope_queries

if TYPE_CHECKING:
    from collections.abc import Callable

    from analytics_toolkit.sql.execution.cancellation import SqlCancellationScope

    from .completion import MetadataProvider
    from .journal import QueryJournal


class JournalMetadataProvider:
    def __init__(
        self,
        provider: MetadataProvider,
        journal: QueryJournal,
        backend: str,
        origin: str,
    ) -> None:
        self.provider = provider
        self.journal = journal
        self.backend = backend
        self.origin = origin

    def resolve_reference(
        self,
        connection_key: str,
        name: str,
        *,
        search_path: list[str] | None = None,
        candidates: list[tuple[str, str]] | None = None,
    ) -> tuple[str, str] | None:
        resolver = getattr(self.provider, "resolve_reference", None)
        if resolver is None:
            return None
        with self.journal.action(
            connection_key, self.backend, self.origin, context={"kind": "resolve", "table": name}
        ):
            result: tuple[str, str] | None = resolver(
                connection_key, name, search_path=search_path, candidates=candidates
            )
            return result

    def _run(self, kind: str, **options: Any) -> tuple[str, ...]:
        with self.journal.action(
            options["connection_key"],
            self.backend,
            self.origin,
            context={"kind": kind, **{k: v for k, v in options.items() if k != "connection_key"}},
        ):
            result: tuple[str, ...] = getattr(self.provider, f"list_{kind}s")(**options)
            return result

    def list_catalogs(self, *, connection_key: str) -> tuple[str, ...]:
        return self._run("catalog", connection_key=connection_key)

    def list_schemas(
        self,
        *,
        connection_key: str,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        return self._run("schema", connection_key=connection_key, catalog=catalog)

    def list_tables(
        self,
        *,
        connection_key: str,
        prefix: str,
        schema: str | None = None,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        return self._run(
            "table", connection_key=connection_key, prefix=prefix, schema=schema, catalog=catalog
        )


def cancel_metadata(
    scope: SqlCancellationScope,
    journal: QueryJournal | None,
    alias: str,
    backend: str,
    cancel: Callable[[SqlCancellationScope], None] = cancel_scope_queries,
) -> None:
    if journal is not None and not scope.aliases:
        return
    with journal.action(alias, backend, "cancel") if journal else nullcontext():
        cancel(scope)
