"""Transactional SQLite storage for one connection alias's query journal."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from threading import Lock, local
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_SCHEMA = (
    "CREATE TABLE actions (action_id TEXT PRIMARY KEY, connection_alias TEXT NOT NULL, "
    "backend TEXT NOT NULL, origin TEXT NOT NULL, is_user INTEGER NOT NULL, "
    "source_file TEXT, user_sql TEXT, context_json TEXT NOT NULL, started_at TEXT NOT NULL, "
    "finished_at TEXT, elapsed_seconds REAL, outcome TEXT NOT NULL, error_json TEXT, "
    "statements_json TEXT NOT NULL, query_types TEXT NOT NULL)",
    "CREATE INDEX action_time ON actions(started_at DESC, action_id DESC)",
    "CREATE INDEX action_user_time ON actions(is_user, started_at DESC, action_id DESC)",
    "CREATE INDEX action_outcome_time ON actions(outcome, started_at DESC, action_id DESC)",
    "CREATE TABLE submissions (action_id TEXT NOT NULL REFERENCES actions(action_id), "
    "submission_index INTEGER NOT NULL, sql TEXT NOT NULL, started_at TEXT NOT NULL, "
    "finished_at TEXT, elapsed_seconds REAL, outcome TEXT NOT NULL, error_json TEXT, "
    "statements_json TEXT NOT NULL, PRIMARY KEY(action_id, submission_index))",
    "CREATE TABLE objects (action_id TEXT NOT NULL REFERENCES actions(action_id), "
    "submission_index INTEGER NOT NULL, statement_index INTEGER NOT NULL, "
    "kind TEXT, catalog TEXT, schema_name TEXT, name TEXT)",
    "CREATE INDEX object_name ON objects(catalog, schema_name, name, action_id)",
    "CREATE INDEX object_action ON objects(action_id, submission_index)",
    "CREATE TABLE search_documents (id INTEGER PRIMARY KEY, "
    "action_id TEXT NOT NULL REFERENCES actions(action_id), "
    "submission_index INTEGER NOT NULL, body TEXT NOT NULL, UNIQUE(action_id, submission_index))",
)
_FTS = (
    "CREATE VIRTUAL TABLE search_fts USING fts5(body, content='search_documents', "
    "content_rowid='id', tokenize='trigram')",
    "CREATE TRIGGER search_insert AFTER INSERT ON search_documents BEGIN "
    "INSERT INTO search_fts(rowid, body) VALUES (new.id, new.body); END",
)
USER_ORIGINS = frozenset({"user", "create_table", "export"})
STORAGE_VERSION = 2


def migrate_completion_events(connection: sqlite3.Connection) -> None:
    """Upgrade v1 in place; export versions are independent of storage versions."""
    connection.execute(
        "CREATE TABLE completion_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
        "action_id TEXT NOT NULL UNIQUE REFERENCES actions(action_id), "
        "historical INTEGER NOT NULL DEFAULT 0)"
    )
    connection.execute(
        "INSERT INTO completion_events(action_id,historical) SELECT action_id,1 FROM actions "
        "WHERE is_user=1 AND outcome='completed' ORDER BY finished_at, action_id"
    )
    connection.execute("PRAGMA user_version=2")


def require_storage_version(version: int) -> None:
    if version != STORAGE_VERSION:
        message = f"Unsupported query journal schema version: {version}"
        raise ValueError(message)


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def query_types(statements: list[dict[str, Any]]) -> str:
    return ", ".join(dict.fromkeys(str(s["action"]) for s in statements))


class JournalStore:
    def __init__(self, path: Path, *, timeout: float = 0.25, use_fts: bool = True) -> None:
        self.path = path
        self.timeout = timeout
        self.use_fts = use_fts
        self._init_lock = Lock()
        self._ready = False
        self._local = local()

    def _initialize(self) -> None:
        with self._init_lock:
            if self._ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.path.exists()
            with closing(sqlite3.connect(str(self.path), timeout=self.timeout)) as connection:
                self.path.chmod(0o600)
                if new:
                    connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    if version == 0 and new:
                        for statement in _SCHEMA:
                            connection.execute(statement)
                        if self.use_fts:
                            self._create_fts(connection)
                        version = 1
                    if version == 1:
                        migrate_completion_events(connection)
                    else:
                        require_storage_version(version)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                self._ready = True

    @staticmethod
    def _create_fts(connection: sqlite3.Connection) -> None:
        connection.execute("SAVEPOINT optional_fts")
        try:
            for statement in _FTS:
                connection.execute(statement)
        except sqlite3.OperationalError:
            connection.execute("ROLLBACK TO optional_fts")
        finally:
            connection.execute("RELEASE optional_fts")

    @contextmanager
    def session(self) -> Iterator[None]:
        """Reuse this worker's connection for an action, with no long transaction."""
        if getattr(self._local, "connection", None) is not None:
            yield
            return
        with self._open(write=True) as connection:
            self._local.connection = connection
            try:
                yield
            finally:
                self._local.connection = None

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = getattr(self._local, "connection", None) if write else None
        if connection is not None:
            with connection:
                yield connection
        else:
            with self._open(write=write) as connection:
                yield connection

    @contextmanager
    def _open(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write or self.path.exists():
            self._initialize()
        uri = self.path.resolve().as_uri() + ("?mode=rw" if write else "?mode=ro")
        with closing(sqlite3.connect(uri, uri=True, timeout=self.timeout)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA cache_size=-512")
            if connection.execute("PRAGMA user_version").fetchone()[0] != STORAGE_VERSION:
                message = "Unsupported query journal schema version."
                raise ValueError(message)
            with connection:
                yield connection

    @staticmethod
    def _index(
        connection: sqlite3.Connection,
        action_id: str,
        index: int,
        statements: list[dict[str, Any]],
        text: str,
    ) -> None:
        rows = [
            (
                action_id,
                index,
                position,
                obj.get("kind"),
                obj.get("catalog"),
                obj.get("schema"),
                obj.get("name"),
            )
            for position, statement in enumerate(statements)
            for obj in statement["objects"]
        ]
        connection.executemany("INSERT INTO objects VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        body = (text + "\n" + encode(statements)).casefold()
        connection.execute(
            "INSERT INTO search_documents(action_id, submission_index, body) VALUES (?, ?, ?)",
            (action_id, index, body),
        )

    def start_action(self, record: dict[str, Any]) -> None:
        with self.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO actions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["action_id"],
                    record["connection_alias"],
                    record["backend"],
                    record["origin"],
                    int(record["origin"] in USER_ORIGINS),
                    record["source_file"],
                    record["user_sql"],
                    encode(record["context"]),
                    record["started_at"],
                    None,
                    None,
                    "running",
                    None,
                    encode(record["statements"]),
                    query_types(record["statements"]),
                ),
            )
            self._index(
                connection,
                record["action_id"],
                -1,
                record["statements"],
                "\n".join(
                    (
                        record["user_sql"] or "",
                        record["source_file"] or "",
                        encode(record["context"]),
                    )
                ),
            )

    def finish_action(self, record: dict[str, Any]) -> None:
        with self.connect(write=True) as connection:
            connection.execute(
                "UPDATE actions SET finished_at=?, elapsed_seconds=?, outcome=?, error_json=? "
                "WHERE action_id=?",
                (
                    record["finished_at"],
                    record["elapsed_seconds"],
                    record["outcome"],
                    encode(record["error"]),
                    record["action_id"],
                ),
            )
            if record["outcome"] == "cancelled":
                connection.execute(
                    "UPDATE submissions SET outcome='cancelled' "
                    "WHERE action_id=? AND outcome='failed'",
                    (record["action_id"],),
                )
            if record["outcome"] == "completed" and record["origin"] in USER_ORIGINS:
                connection.execute(
                    "INSERT OR IGNORE INTO completion_events(action_id) VALUES (?)",
                    (record["action_id"],),
                )

    def completed_since(self, sequence: int, limit: int = 100) -> list[dict[str, Any]]:
        """Read bounded metadata-only events in commit order, without loading SQL."""
        if not self.path.exists():
            return []
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT e.sequence, e.historical, a.action_id, a.backend, a.origin, a.finished_at, "
                "a.context_json, a.statements_json FROM completion_events e "
                "JOIN actions a USING(action_id) WHERE e.sequence>? ORDER BY e.sequence LIMIT ?",
                (sequence, limit),
            ).fetchall()
            result = []
            for row in rows:
                event = dict(row)
                event["context"] = json.loads(event.pop("context_json"))
                event["statements"] = json.loads(event.pop("statements_json"))
                if event["origin"] == "create_table":
                    event["statements"] = [
                        statement
                        for submission in connection.execute(
                            "SELECT statements_json FROM submissions WHERE action_id=? "
                            "AND outcome='completed' ORDER BY submission_index",
                            (event["action_id"],),
                        )
                        for statement in json.loads(submission[0])
                        if statement["action"] in {"create", "alter", "drop", "rename", "insert"}
                    ]
                result.append(event)
            return result

    def start_submission(self, action_id: str, item: dict[str, Any]) -> None:
        with self.connect(write=True) as connection:
            connection.execute(
                "INSERT INTO submissions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    action_id,
                    item["index"],
                    item["sql"],
                    item["started_at"],
                    None,
                    None,
                    "running",
                    None,
                    encode(item["statements"]),
                ),
            )
            self._index(connection, action_id, item["index"], item["statements"], item["sql"])
            connection.execute(
                "UPDATE actions SET query_types=? WHERE action_id=? AND query_types=''",
                (query_types(item["statements"]), action_id),
            )

    def finish_submission(self, action_id: str, item: dict[str, Any]) -> None:
        with self.connect(write=True) as connection:
            connection.execute(
                "UPDATE submissions SET finished_at=?, elapsed_seconds=?, outcome=?, error_json=? "
                "WHERE action_id=? AND submission_index=?",
                (
                    item["finished_at"],
                    item["elapsed_seconds"],
                    item["outcome"],
                    encode(item["error"]),
                    action_id,
                    item["index"],
                ),
            )

    def load(self, action_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            # Keep action and submissions in the same read snapshot.
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM actions WHERE action_id=?", (action_id,)
            ).fetchone()
            if row is None:
                message = "The selected journal entry no longer exists."
                raise ValueError(message)
            record = dict(row)
            record.pop("is_user")
            record.pop("query_types")
            record.update(schema_version=1)
            for name in ("context", "statements", "error"):
                record[name] = json.loads(record.pop(name + "_json") or "null")
            submissions = []
            for row in connection.execute(
                "SELECT * FROM submissions WHERE action_id=? ORDER BY submission_index",
                (action_id,),
            ):
                item = dict(row)
                item.pop("action_id")
                item["index"] = item.pop("submission_index")
                for name in ("statements", "error"):
                    item[name] = json.loads(item.pop(name + "_json") or "null")
                submissions.append(item)
            record["submissions"] = submissions
            return record
