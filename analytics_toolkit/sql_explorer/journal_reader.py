"""Indexed history pages and lazy action details."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Tuple

if TYPE_CHECKING:
    from .journal import QueryJournal
    from .journal_store import JournalStore

JournalCursor = Tuple[str, str]
_MIN_TRIGRAM_LENGTH = 3


@dataclass(frozen=True)
class JournalPage:
    records: tuple[dict[str, Any], ...]
    cursor: JournalCursor | None
    warning: str


def _page_query(  # noqa: PLR0913 -- storage connection and independent filters.
    connection: sqlite3.Connection,
    store: JournalStore,
    *,
    query: str,
    include_internal: bool,
    before: JournalCursor | None,
    limit: int,
) -> list[sqlite3.Row]:
    filters = []
    arguments: list[Any] = []
    if not include_internal:
        filters.append("a.is_user=1")
    if before:
        filters.append("(a.started_at, a.action_id) < (?, ?)")
        arguments.extend(before)
    search = query.casefold()
    use_fts = bool(
        store.use_fts
        and len(search) >= _MIN_TRIGRAM_LENGTH
        and "\x00" not in search
        and connection.execute("SELECT 1 FROM sqlite_master WHERE name='search_fts'").fetchone()
    )
    search_filter = (
        "EXISTS (SELECT 1 FROM search_documents d WHERE d.action_id=a.action_id "
        "AND instr(d.body, ?) > 0{fts})"
    )
    if search:
        filters.append(search_filter)
        arguments.append(search)
    base = (
        "SELECT a.action_id, a.connection_alias, a.origin, a.source_file, a.started_at, "
        "a.finished_at, a.outcome, a.elapsed_seconds, a.query_types FROM actions a"
    )
    if filters:
        base += " WHERE " + " AND ".join(filters)
    base += " ORDER BY a.started_at DESC, a.action_id DESC LIMIT ?"
    if use_fts:
        try:
            return connection.execute(
                base.format(
                    fts=" AND d.id IN (SELECT rowid FROM search_fts WHERE search_fts MATCH ?)"
                ),
                (*arguments, '"' + search.replace('"', '""') + '"', limit + 1),
            ).fetchall()
        except sqlite3.OperationalError:
            # An existing database may be opened by a build without this tokenizer.
            pass
    return connection.execute(base.format(fts=""), (*arguments, limit + 1)).fetchall()


def read_page(  # noqa: PLR0913 -- independent paging and filtering inputs.
    journal: QueryJournal,
    alias: str,
    *,
    query: str = "",
    include_internal: bool = False,
    before: JournalCursor | None = None,
    limit: int = 100,
) -> JournalPage:
    store = journal.store(alias)
    try:
        if not store.path.exists():
            return JournalPage((), None, "")
        with store.connect() as connection:
            rows = _page_query(
                connection,
                store,
                query=query,
                include_internal=include_internal,
                before=before,
                limit=limit,
            )
        records = tuple(dict(row) for row in rows[:limit])
        cursor = (
            (records[-1]["started_at"], records[-1]["action_id"]) if len(rows) > limit else None
        )
        return JournalPage(records, cursor, "")
    except (OSError, sqlite3.Error, ValueError) as exc:
        return JournalPage((), None, f"Cannot read journal: {exc}")


def read_record(journal: QueryJournal, alias: str, action_id: str) -> dict[str, Any]:
    return journal.store(alias).load(action_id)
