"""SQL keyword and serialized backend-metadata completion."""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, Final, Literal, Protocol
from uuid import uuid4

from analytics_toolkit import sql
from analytics_toolkit.sql.backends.utils import sql_literal
from analytics_toolkit.sql.ddl.identifiers import quote_identifier

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    import pandas as pd

CompletionKind = Literal["keyword", "table", "catalog", "schema"]

KEYWORDS: Final[tuple[str, ...]] = (
    "SELECT",
    "FROM",
    "WHERE",
    "GROUP BY",
    "ORDER BY",
    "HAVING",
    "JOIN",
    "LEFT JOIN",
    "RIGHT JOIN",
    "INNER JOIN",
    "LIMIT",
    "INSERT INTO",
    "UPDATE",
    "DELETE FROM",
    "CREATE TABLE",
)
MIN_TABLE_PREFIX_LENGTH: Final[int] = 6
_CATALOG_SCHEMA_TABLE_PARTS: Final[int] = 3
_SCHEMA_TABLE_PARTS: Final[int] = 2


class MetadataProvider(Protocol):
    def list_tables(
        self,
        *,
        connection_key: str,
        prefix: str,
        schema: str | None = None,
        catalog: str | None = None,
    ) -> tuple[str, ...]: ...

    def list_catalogs(self, *, connection_key: str) -> tuple[str, ...]: ...

    def list_schemas(
        self,
        *,
        connection_key: str,
        catalog: str | None = None,
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class CompletionRequest:
    connection_key: str
    backend: str
    kind: CompletionKind
    prefix: str
    schema: str | None = None
    catalog: str | None = None
    database: str | None = None
    context: str = ""

    @property
    def scope(
        self,
    ) -> tuple[str, str, CompletionKind, str | None, str | None, str | None, str]:
        """Cache scope; the typed prefix is intentionally filtered locally."""
        return (
            self.connection_key,
            self.backend,
            self.kind,
            self.catalog,
            self.database,
            self.schema,
            self.context,
        )

    @property
    def identity(
        self,
    ) -> tuple[str, str, CompletionKind, str | None, str | None, str | None, str, str]:
        return (*self.scope, self.prefix.casefold())


@dataclass(frozen=True)
class CompletionContext:
    request: CompletionRequest
    replacement_start: int
    replacement_end: int
    raw_prefix: str
    normalized_prefix: str

    @property
    def table_context(self) -> bool:
        return self.request.kind == "table"


@dataclass(frozen=True)
class CompletionResult:
    request: CompletionRequest
    request_id: int
    suggestions: tuple[str, ...]


@dataclass(frozen=True)
class CompletionCacheEntry:
    kind: CompletionKind
    values: tuple[str, ...]


@dataclass(frozen=True)
class _CompletionTask:
    request: CompletionRequest
    request_id: int
    on_success: Callable[[CompletionResult], None]
    on_error: Callable[[CompletionResult, Exception], None]
    bootstrap: bool = False


def normalize_identifier_prefix(prefix: str) -> str:
    return prefix.strip().strip("\"'")


def normalize_completion_values(values: Iterable[object]) -> tuple[str, ...]:
    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        candidate = str(value).strip()
        identity = candidate.casefold()
        if not candidate or identity in seen:
            continue
        seen.add(identity)
        normalized.append(candidate)
    return tuple(sorted(normalized, key=str.casefold))


def filter_suggestions(values: Sequence[str], prefix: str) -> tuple[str, ...]:
    normalized = normalize_identifier_prefix(prefix).casefold()
    return tuple(value for value in values if value.casefold().startswith(normalized))


def keyword_suggestions(prefix: str) -> tuple[str, ...]:
    return filter_suggestions(KEYWORDS, prefix)


def _metadata_frame(connection_key: str, query: str) -> pd.DataFrame:
    return sql.read(
        connection_key,
        query,
        retry_cnt=1,
        timeout_increment=0,
        query_label=f"sql_explorer metadata={uuid4().hex}",
    )


def _first_column_values(frame: pd.DataFrame) -> tuple[str, ...]:
    if frame.empty:
        return ()
    return normalize_completion_values(frame[frame.columns[0]].tolist())


def _table_condition(prefix: str) -> str:
    return f"lower(table_name) LIKE lower({sql_literal(prefix + '%')})"


class GreenplumCompletionProvider:
    def list_tables(
        self,
        *,
        connection_key: str,
        prefix: str,
        schema: str | None = None,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        del catalog
        frame = sql.show_tables(
            connection_key,
            schema=schema,
            conditions=_table_condition(prefix),
        )
        return normalize_completion_values(frame["table_name"].tolist())

    def list_catalogs(self, *, connection_key: str) -> tuple[str, ...]:
        del connection_key
        return ()

    def list_schemas(
        self,
        *,
        connection_key: str,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        del catalog
        return _first_column_values(
            _metadata_frame(
                connection_key,
                "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name",
            )
        )


class ClickHouseCompletionProvider:
    def list_tables(
        self,
        *,
        connection_key: str,
        prefix: str,
        schema: str | None = None,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        del catalog
        frame = sql.show_tables(
            connection_key,
            schema=schema,
            conditions=_table_condition(prefix),
        )
        return normalize_completion_values(frame["table_name"].tolist())

    def list_catalogs(self, *, connection_key: str) -> tuple[str, ...]:
        del connection_key
        return ()

    def list_schemas(
        self,
        *,
        connection_key: str,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        del catalog
        return _first_column_values(_metadata_frame(connection_key, "SHOW DATABASES"))


class TrinoCompletionProvider:
    def list_tables(
        self,
        *,
        connection_key: str,
        prefix: str,
        schema: str | None = None,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        frame = sql.show_tables(
            connection_key,
            schema=schema,
            conditions=_table_condition(prefix),
            trino_catalog=catalog,
        )
        return normalize_completion_values(frame["table_name"].tolist())

    def list_catalogs(self, *, connection_key: str) -> tuple[str, ...]:
        return _first_column_values(_metadata_frame(connection_key, "SHOW CATALOGS"))

    def list_schemas(
        self,
        *,
        connection_key: str,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        query = "SHOW SCHEMAS"
        if catalog is not None:
            query += f" FROM {quote_identifier(catalog, 'trino')}"
        return _first_column_values(_metadata_frame(connection_key, query))


def provider_for_backend(backend: str) -> MetadataProvider:
    normalized = backend.casefold()
    if normalized == "trino":
        return TrinoCompletionProvider()
    if normalized in {"ch", "clickhouse"}:
        return ClickHouseCompletionProvider()
    return GreenplumCompletionProvider()


@dataclass
class CompletionCoordinator:
    """One-worker FIFO metadata queue with context-scoped local filtering."""

    connection_key: str
    backend: str
    provider: MetadataProvider | None = None
    _queue: deque[_CompletionTask] = field(default_factory=deque, init=False)
    _queued_scopes: set[tuple[object, ...]] = field(default_factory=set, init=False)
    _cache: dict[tuple[object, ...], CompletionCacheEntry] = field(
        default_factory=dict,
        init=False,
    )
    _catalogs: tuple[str, ...] | None = field(default=None, init=False)
    _schemas: dict[str | None, tuple[str, ...]] = field(default_factory=dict, init=False)
    _request_seq: int = field(default=0, init=False)
    _in_flight: tuple[object, ...] | None = field(default=None, init=False)
    _lock: Lock = field(default_factory=Lock, init=False)
    _wake: Event = field(default_factory=Event, init=False)
    _stopping: bool = field(default=False, init=False)
    _thread: Thread = field(init=False)
    _bootstrap_error: Callable[[CompletionResult, Exception], None] | None = field(
        default=None,
        init=False,
    )

    def __post_init__(self) -> None:
        self.backend = self.backend.casefold()
        self.provider = self.provider or provider_for_backend(self.backend)
        self._thread = Thread(target=self._worker, name="sql-explorer-metadata", daemon=True)
        self._thread.start()

    def start_bootstrap(
        self,
        *,
        on_error: Callable[[CompletionResult, Exception], None] | None = None,
    ) -> None:
        """Eagerly queue Trino catalogs; schemas are appended after discovery."""
        if self.backend != "trino" or self._catalogs is not None:
            return
        self._bootstrap_error = on_error
        self.enqueue(
            CompletionRequest(
                self.connection_key,
                self.backend,
                "catalog",
                "",
                context="startup",
            ),
            on_error=on_error,
            bootstrap=True,
        )

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            self._queue.clear()
            self._queued_scopes.clear()
        self._wake.set()

    def enqueue(
        self,
        request: CompletionRequest,
        *,
        on_success: Callable[[CompletionResult], None] | None = None,
        on_error: Callable[[CompletionResult, Exception], None] | None = None,
        bootstrap: bool = False,
    ) -> int:
        on_success = on_success or (lambda _result: None)
        on_error = on_error or (lambda _result, _exc: None)
        request_id = self._next_id()
        if request.kind == "keyword":
            on_success(CompletionResult(request, request_id, keyword_suggestions(request.prefix)))
            return request_id

        cached = self.cached(request)
        if cached is not None:
            on_success(CompletionResult(request, request_id, cached))
            return request_id

        scope = request.scope
        with self._lock:
            if scope in self._queued_scopes or self._in_flight == scope:
                return request_id
            self._queued_scopes.add(scope)
            self._queue.append(
                _CompletionTask(request, request_id, on_success, on_error, bootstrap)
            )
        self._wake.set()
        return request_id

    def cached(self, request: CompletionRequest) -> tuple[str, ...] | None:
        with self._lock:
            entry = self._cache.get(request.scope)
        if entry is None:
            return None
        return filter_suggestions(entry.values, request.prefix)

    def known_catalogs(self) -> tuple[str, ...] | None:
        with self._lock:
            return self._catalogs

    def cached_schemas(self, catalog: str | None = None) -> tuple[str, ...] | None:
        with self._lock:
            return self._schemas.get(catalog)

    def enqueue_schemas(
        self,
        *,
        catalog: str | None = None,
        on_success: Callable[[CompletionResult], None] | None = None,
        on_error: Callable[[CompletionResult, Exception], None] | None = None,
    ) -> int:
        return self.enqueue(
            CompletionRequest(
                self.connection_key,
                self.backend,
                "schema",
                "",
                catalog=catalog,
                context="namespace",
            ),
            on_success=on_success,
            on_error=on_error,
            bootstrap=self.backend == "trino",
        )

    def snapshot(self) -> tuple[int, int, bool]:
        with self._lock:
            return len(self._cache), len(self._queue), self._in_flight is not None

    def _next_id(self) -> int:
        with self._lock:
            self._request_seq += 1
            return self._request_seq

    def _worker(self) -> None:
        while True:
            self._wake.wait()
            task = self._take_task()
            if task is None:
                if self._stopping:
                    return
                continue
            try:
                values = normalize_completion_values(self._run(task.request))
            except Exception as exc:  # noqa: BLE001 -- isolated metadata notice.
                task.on_error(CompletionResult(task.request, task.request_id, ()), exc)
            else:
                self._store_result(task.request, values)
                task.on_success(CompletionResult(task.request, task.request_id, values))
                if task.bootstrap and task.request.kind == "catalog":
                    for catalog in values:
                        self.enqueue_schemas(
                            catalog=catalog,
                            on_error=self._bootstrap_error,
                        )
            finally:
                with self._lock:
                    self._in_flight = None
                    has_more = bool(self._queue)
                if has_more:
                    self._wake.set()

    def _take_task(self) -> _CompletionTask | None:
        with self._lock:
            if self._stopping or not self._queue:
                self._wake.clear()
                return None
            task = self._queue.popleft()
            self._queued_scopes.discard(task.request.scope)
            self._in_flight = task.request.scope
            if not self._queue:
                self._wake.clear()
            return task

    def _store_result(
        self,
        request: CompletionRequest,
        values: tuple[str, ...],
    ) -> None:
        with self._lock:
            self._cache[request.scope] = CompletionCacheEntry(request.kind, values)
            if request.kind == "catalog":
                self._catalogs = values
            elif request.kind == "schema":
                self._schemas[request.catalog] = values

    def _run(self, request: CompletionRequest) -> tuple[str, ...]:
        provider = self.provider
        if provider is None:  # pragma: no cover - initialized in __post_init__.
            message = "Metadata completion provider is unavailable."
            raise RuntimeError(message)
        if request.kind == "catalog":
            return provider.list_catalogs(connection_key=request.connection_key)
        if request.kind == "schema":
            return provider.list_schemas(
                connection_key=request.connection_key,
                catalog=request.catalog,
            )
        if request.kind == "table":
            return provider.list_tables(
                connection_key=request.connection_key,
                prefix=request.prefix,
                schema=request.schema,
                catalog=request.catalog,
            )
        return keyword_suggestions(request.prefix)


_TABLE_CONTEXT_RE: Final = re.compile(r"(?is)(?:^|.*\b)(from|join|update|insert\s+into|into)\s*$")
_TOKEN_RE: Final = re.compile(r'([A-Za-z0-9_."]+)$')


def parse_completion_context(
    text: str,
    cursor_offset: int,
    *,
    backend: str,
    connection_key: str = "",
    trino_catalogs: tuple[str, ...] | None = None,
) -> CompletionContext:
    """Return completion scope and the final identifier fragment to replace."""
    cursor_offset = max(0, min(cursor_offset, len(text)))
    before = text[:cursor_offset]
    line_start = before.rfind("\n") + 1
    line = before[line_start:]
    token_match = _TOKEN_RE.search(line)
    raw_token = token_match.group(1) if token_match else ""
    final_prefix = raw_token.rsplit(".", 1)[-1]
    replacement_end = cursor_offset
    replacement_start = cursor_offset - len(final_prefix)
    before_token = line[: len(line) - len(raw_token)]
    context_match = _TABLE_CONTEXT_RE.match(before_token)
    normalized_backend = backend.casefold()

    if context_match is None:
        request = CompletionRequest(
            connection_key,
            normalized_backend,
            "keyword",
            normalize_identifier_prefix(final_prefix),
            context=f"keyword:{line_start}",
        )
        return CompletionContext(
            request,
            replacement_start,
            replacement_end,
            final_prefix,
            request.prefix,
        )

    clause = " ".join(context_match.group(1).casefold().split())
    context = f"{clause}:{line_start + context_match.start(1)}"
    token = normalize_identifier_prefix(raw_token).replace('"', "")
    parts = token.split(".") if token else [""]
    catalog: str | None = None
    schema: str | None = None
    database: str | None = None

    if normalized_backend == "trino":
        if len(parts) >= _CATALOG_SCHEMA_TABLE_PARTS:
            catalog, schema = parts[0], parts[1]
        elif len(parts) == _SCHEMA_TABLE_PARTS:
            if trino_catalogs and parts[0] in trino_catalogs:
                catalog = parts[0]
            else:
                schema = parts[0]
    elif len(parts) >= _SCHEMA_TABLE_PARTS:
        schema = parts[-2]
        if normalized_backend in {"ch", "clickhouse"}:
            database = schema

    prefix = normalize_identifier_prefix(parts[-1])
    request = CompletionRequest(
        connection_key,
        normalized_backend,
        "table",
        prefix,
        schema=schema,
        catalog=catalog,
        database=database,
        context=context,
    )
    return CompletionContext(
        request,
        replacement_start,
        replacement_end,
        final_prefix,
        prefix,
    )


__all__ = [
    "KEYWORDS",
    "MIN_TABLE_PREFIX_LENGTH",
    "ClickHouseCompletionProvider",
    "CompletionCacheEntry",
    "CompletionContext",
    "CompletionCoordinator",
    "CompletionRequest",
    "CompletionResult",
    "GreenplumCompletionProvider",
    "MetadataProvider",
    "TrinoCompletionProvider",
    "filter_suggestions",
    "keyword_suggestions",
    "normalize_completion_values",
    "normalize_identifier_prefix",
    "parse_completion_context",
    "provider_for_backend",
]
