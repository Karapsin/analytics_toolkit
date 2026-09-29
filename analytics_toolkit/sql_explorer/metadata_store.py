"""Private, transactional names-only snapshots for SQL Explorer."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from time import time
from typing import TYPE_CHECKING, Dict, Tuple

if TYPE_CHECKING:
    from pathlib import Path

SnapshotKey = Tuple[str, str, str]
Snapshots = Dict[SnapshotKey, Tuple[str, ...]]


class MetadataStore:
    def __init__(self, directory: Path, identity: str) -> None:
        self.path = directory / "metadata.sqlite3"
        self.identity = identity

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=1)
        try:
            self.path.chmod(0o600)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS snapshots ("
                "identity TEXT, kind TEXT, catalog TEXT, schema_name TEXT, "
                "names TEXT NOT NULL, refreshed_at REAL NOT NULL, "
                "PRIMARY KEY (identity, kind, catalog, schema_name))"
            )
        except Exception:
            connection.close()
            raise
        return connection

    def load(self) -> Snapshots:
        if not self.path.exists():
            return {}
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT kind, catalog, schema_name, names FROM snapshots WHERE identity = ?",
                (self.identity,),
            )
            snapshots = {}
            for kind, catalog, schema, payload in rows:
                names = json.loads(payload)
                if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                    message = "Invalid cached metadata names."
                    raise ValueError(message)
                snapshots[(kind, catalog, schema)] = tuple(names)
            return snapshots

    def save(self, snapshots: Snapshots) -> None:
        """Replace one connection's snapshots in a single transaction."""
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM snapshots WHERE identity = ?", (self.identity,))
            connection.executemany(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (self.identity, *key, json.dumps(names), time())
                    for key, names in snapshots.items()
                ],
            )

    def update(self, key: SnapshotKey, names: tuple[str, ...]) -> None:
        """Commit one schema without rewriting every other schema's table names."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?, ?)",
                (self.identity, *key, json.dumps(names), time()),
            )
            kind, catalog, _schema = key
            if kind == "table":
                return
            keys = connection.execute(
                "SELECT kind, catalog, schema_name FROM snapshots WHERE identity = ?",
                (self.identity,),
            ).fetchall()
            removed = [
                (self.identity, *candidate)
                for candidate in keys
                if (kind == "catalog" and candidate[0] != "catalog" and candidate[1] not in names)
                or (
                    kind == "schema"
                    and candidate[0] == "table"
                    and candidate[1] == catalog
                    and candidate[2] not in names
                )
            ]
            connection.executemany(
                "DELETE FROM snapshots WHERE identity = ? AND kind = ? "
                "AND catalog = ? AND schema_name = ?",
                removed,
            )
