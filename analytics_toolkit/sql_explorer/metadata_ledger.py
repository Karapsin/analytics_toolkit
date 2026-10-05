"""Durable scan scheduling, leases, object usage and journal replay checkpoints."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Tuple, cast
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from .metadata_store import MetadataStore, SnapshotKey

REFRESH_SECONDS = 24 * 60 * 60
LEASE_SECONDS = 300
ObjectKey = Tuple[str, str, str, str]


@dataclass(frozen=True)
class ScanTicket:
    key: SnapshotKey
    sequence: int
    owner: str


class MetadataLedger:
    def __init__(self, store: MetadataStore | None) -> None:
        self.store = store
        self.identity = store.identity if store else "memory"
        self.owner = uuid4().hex
        self._lock = RLock()
        self._memory = (
            sqlite3.connect(":memory:", check_same_thread=False) if store is None else None
        )
        with self.transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                message = f"Unsupported metadata schema version: {version}"
                raise ValueError(message)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS scan_state (identity TEXT, kind TEXT, catalog TEXT, "
                "schema_name TEXT, scanned_at REAL, last_query_at REAL NOT NULL DEFAULT 0, "
                "used_sequence INTEGER NOT NULL DEFAULT 0, covered_sequence INTEGER NOT NULL "
                "DEFAULT 0, "
                "urgent_sequence INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '', "
                "failures INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0, "
                "owner TEXT, lease_until REAL NOT NULL DEFAULT 0, "
                "PRIMARY KEY(identity, kind, catalog, schema_name))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS object_usage (identity TEXT, kind TEXT, catalog TEXT, "
                "schema_name TEXT, name TEXT, last_query_at REAL NOT NULL DEFAULT 0, "
                "PRIMARY KEY(identity, kind, catalog, schema_name, name))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS journal_checkpoint (identity TEXT PRIMARY KEY, "
                "sequence INTEGER NOT NULL)"
            )
            if store:
                connection.execute(
                    "INSERT OR IGNORE INTO scan_state(identity, kind, catalog, schema_name, "
                    "scanned_at) "
                    "SELECT identity, kind, catalog, schema_name, refreshed_at FROM snapshots"
                )
                for row in connection.execute(
                    "SELECT kind,catalog,schema_name,names FROM snapshots WHERE identity=?",
                    (self.identity,),
                ).fetchall():
                    self._record_names(connection, (row[0], row[1], row[2]), json.loads(row[3]))
            connection.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = (
                self.store.connect() if self.store else cast("sqlite3.Connection", self._memory)
            )
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("BEGIN IMMEDIATE")
                with connection:
                    yield connection
            finally:
                if self.store:
                    connection.close()

    def ensure(self, keys: Sequence[SnapshotKey]) -> None:
        with self.transaction() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO scan_state(identity, kind, catalog, schema_name) VALUES "
                "(?,?,?,?)",
                [(self.identity, *key) for key in keys],
            )

    def checkpoint(self) -> int:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT sequence FROM journal_checkpoint WHERE identity=?", (self.identity,)
            ).fetchone()
            return int(row[0]) if row else 0

    def consume(
        self,
        sequence: int,
        used_at: float,
        scopes: dict[SnapshotKey, str],
        objects: Sequence[ObjectKey],
        *,
        historical: bool = False,
    ) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT sequence FROM journal_checkpoint WHERE identity=?", (self.identity,)
            ).fetchone()
            if row and row[0] >= sequence:
                return False
            for key, reason in scopes.items():
                connection.execute(
                    "INSERT OR IGNORE INTO scan_state(identity,kind,catalog,schema_name) VALUES "
                    "(?,?,?,?)",
                    (self.identity, *key),
                )
                connection.execute(
                    "UPDATE scan_state SET last_query_at=MAX(last_query_at,?) WHERE identity=? "
                    "AND kind=? AND catalog=? AND schema_name=?",
                    (used_at, self.identity, *key),
                )
                connection.execute(
                    "UPDATE scan_state SET last_query_at=MAX(last_query_at,?), "
                    "used_sequence=MAX(used_sequence,?), "
                    "urgent_sequence=CASE WHEN ?!='' THEN ? ELSE urgent_sequence END, "
                    "reason=CASE WHEN ?!='' THEN ? ELSE reason END "
                    "WHERE identity=? AND kind=? AND catalog=? AND schema_name=? "
                    "AND (?=0 OR scanned_at IS NULL OR ? > scanned_at)",
                    (
                        used_at,
                        sequence,
                        reason,
                        sequence,
                        reason,
                        reason,
                        self.identity,
                        *key,
                        int(historical),
                        used_at,
                    ),
                )
            for obj in objects:
                connection.execute(
                    "INSERT INTO object_usage VALUES (?,?,?,?,?,?) ON CONFLICT DO UPDATE "
                    "SET last_query_at=MAX(last_query_at,excluded.last_query_at)",
                    (self.identity, *obj, used_at),
                )
            connection.execute(
                "INSERT OR REPLACE INTO journal_checkpoint VALUES (?,?)", (self.identity, sequence)
            )
            return True

    def claim(self, now: float) -> ScanTicket | None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM scan_state WHERE identity=? AND lease_until<=? AND retry_at<=? "
                "AND (scanned_at IS NULL OR urgent_sequence>covered_sequence "
                "OR (used_sequence>covered_sequence AND scanned_at+?<=?)) "
                "ORDER BY (urgent_sequence>covered_sequence) DESC, "
                "CASE kind WHEN 'catalog' THEN 0 WHEN 'schema' THEN 1 ELSE 2 END, "
                "catalog,schema_name LIMIT 1",
                (self.identity, now, now, REFRESH_SECONDS, now),
            ).fetchone()
            if row is None:
                return None
            key = (row["kind"], row["catalog"], row["schema_name"])
            connection.execute(
                "UPDATE scan_state SET owner=?,lease_until=? WHERE identity=? AND kind=? "
                "AND catalog=? AND schema_name=?",
                (self.owner, now + LEASE_SECONDS, self.identity, *key),
            )
            return ScanTicket(key, row["used_sequence"], self.owner)

    def renew(self, ticket: ScanTicket, now: float) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE scan_state SET lease_until=? WHERE identity=? AND kind=? AND catalog=? "
                "AND schema_name=? AND owner=?",
                (now + LEASE_SECONDS, self.identity, *ticket.key, ticket.owner),
            )

    def finish(self, ticket: ScanTicket, names: tuple[str, ...], now: float) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT urgent_sequence,owner FROM scan_state WHERE identity=? AND kind=? "
                "AND catalog=? AND schema_name=?",
                (self.identity, *ticket.key),
            ).fetchone()
            if row is None or row["owner"] != ticket.owner:
                return False
            if row["urgent_sequence"] > ticket.sequence:
                self._release(connection, ticket)
                return False
            if self.store:
                self.store.write_snapshot(connection, ticket.key, names, now)
            connection.execute(
                "UPDATE scan_state SET scanned_at=?,covered_sequence=?,failures=0,retry_at=0, "
                "reason='',owner=NULL,lease_until=0 WHERE identity=? AND kind=? AND catalog=? "
                "AND schema_name=?",
                (now, ticket.sequence, self.identity, *ticket.key),
            )
            kind, catalog, _schema = ticket.key
            self._record_names(connection, ticket.key, names)
            # Remove stale descendant schedules together with their snapshots.
            if kind in {"catalog", "schema"}:
                children = connection.execute(
                    "SELECT kind,catalog,schema_name,scanned_at FROM scan_state WHERE identity=?",
                    (self.identity,),
                ).fetchall()
                removed = [
                    (self.identity, *child[:3])
                    for child in children
                    if child[3] is not None
                    and (
                        (kind == "catalog" and child[0] != "catalog" and child[1] not in names)
                        or (
                            kind == "schema"
                            and child[0] == "table"
                            and child[1] == catalog
                            and child[2] not in names
                        )
                    )
                ]
                connection.executemany(
                    "DELETE FROM scan_state WHERE identity=? AND kind=? AND catalog=? AND "
                    "schema_name=?",
                    removed,
                )
            return True

    def _record_names(
        self, connection: sqlite3.Connection, key: SnapshotKey, names: Sequence[str]
    ) -> None:
        kind, catalog, schema = key
        connection.executemany(
            "INSERT OR IGNORE INTO object_usage VALUES (?,?,?,?,?,0)",
            [
                (
                    self.identity,
                    kind,
                    name if kind == "catalog" else catalog,
                    name if kind == "schema" else schema,
                    name,
                )
                for name in names
            ],
        )

    def _release(self, connection: sqlite3.Connection, ticket: ScanTicket) -> None:
        connection.execute(
            "UPDATE scan_state SET owner=NULL,lease_until=0 WHERE identity=? AND kind=? "
            "AND catalog=? AND schema_name=? AND owner=?",
            (self.identity, *ticket.key, ticket.owner),
        )

    def failed(self, ticket: ScanTicket, now: float, *, cancelled: bool = False) -> None:
        with self.transaction() as connection:
            if not cancelled:
                connection.execute(
                    "UPDATE scan_state SET retry_at=?+MIN(3600,60*(1 << MIN(failures,6))), "
                    "failures=failures+1 WHERE identity=? AND kind=? AND catalog=? "
                    "AND schema_name=? AND owner=?",
                    (now, self.identity, *ticket.key, ticket.owner),
                )
            self._release(connection, ticket)

    def close(self) -> None:
        if self._memory:
            self._memory.close()
