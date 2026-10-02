from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit.sql.execution.observation import observed_connection
from analytics_toolkit.sql_explorer.journal import QueryJournal
from analytics_toolkit.sql_explorer.journal_exports import export_record, export_text
from analytics_toolkit.sql_explorer.journal_reader import read_page, read_record
from analytics_toolkit.sql_explorer.journal_store import JournalStore

from tests.sql.explorer.journal import Driver

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("use_fts", [True, False])
def test_search_has_literal_substring_semantics_with_both_engines(
    tmp_path: Path, use_fts: bool
) -> None:
    journal = QueryJournal(tmp_path)
    store = journal.store("gp")
    store.use_fts = use_fts
    sql = '-- Straße; "quoted"; 100%; _; ЖУРНАЛ\nselect * from sales.orders'
    with journal.action("gp", "gp", "user", user_sql=sql, source_file="report.sql"):
        observed_connection(Driver()).execute("select * from sales.orders LIMIT 201")
    for term in (
        "STRASSE",
        '"quoted"',
        "100%",
        "_",
        "журнал",
        "201",
        "report.sql",
        "sales",
        "or",
        "s",
        "",
    ):
        assert len(read_page(journal, "gp", query=term).records) == 1, term
    assert not read_page(journal, "gp", query="absent").records
    assert not read_page(journal, "gp", query="\x00").records
    with store.connect() as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT action_id FROM actions WHERE is_user=1 "
            "ORDER BY started_at DESC, action_id DESC LIMIT 100"
        ).fetchall()
        assert any("action_user_time" in row[3] for row in plan)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("SELECT name FROM objects WHERE name='orders'").fetchall()
        if not use_fts:
            assert not connection.execute(
                "SELECT name FROM sqlite_master WHERE name='search_fts'"
            ).fetchall()


def test_paging_is_lightweight_and_reopens_persistent_data(tmp_path: Path) -> None:
    journal = QueryJournal(tmp_path)
    text = "select 1 -- " + "a" * 10000
    with journal.action("gp", "gp", "user", user_sql=text):
        pass
    reopened = QueryJournal(tmp_path)
    summary = read_page(reopened, "gp").records[0]
    assert "user_sql" not in summary
    assert "submissions" not in summary
    record = read_record(reopened, "gp", summary["action_id"])
    assert record["user_sql"] == text
    assert export_text(record, ".sql") == text
    assert json.loads(export_text(record, ".json")) == record
    with reopened.action("gp", "gp", "background") as action:
        pass
    internal = read_record(reopened, "gp", action.record["action_id"])
    with pytest.raises(ValueError, match="no user-visible"):
        export_text(internal, ".sql")
    with pytest.raises(ValueError, match="no longer exists"):
        read_record(reopened, "gp", "absent")


def test_schema_mismatch_and_corruption_are_not_recreated(tmp_path: Path) -> None:
    journal = QueryJournal(tmp_path)
    store = journal.store("gp")
    store.path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(str(store.path))) as connection:
        connection.execute("PRAGMA user_version=99")
    with journal.action("gp", "gp", "user"):
        pass
    assert journal.take_warning()
    assert "schema version" in read_page(journal, "gp").warning
    with closing(sqlite3.connect(str(store.path))) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


def test_transaction_rolls_back_action_and_indexes_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = QueryJournal(tmp_path)
    store = journal.store("gp")
    original = store._index

    def fail_index(*args: object) -> None:
        original(*args)
        message = "index failure"
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(store, "_index", fail_index)
    with journal.action("gp", "gp", "user", user_sql="select 1"):
        pass
    assert journal.take_warning()
    assert not read_page(journal, "gp").records
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM search_documents").fetchone()[0] == 0


def test_busy_writer_warns_and_does_not_block_query(tmp_path: Path) -> None:
    journal = QueryJournal(tmp_path)
    with journal.action("gp", "gp", "user", user_sql="select 1"):
        pass
    journal.store("gp").timeout = 0.01
    with journal.store("gp").connect(write=True) as locked:
        locked.execute("BEGIN IMMEDIATE")
        with journal.action("gp", "gp", "user", user_sql="select 2"):
            assert observed_connection(Driver()).command("select 2") == "ok"
    assert "locked" in (journal.take_warning() or "")
    assert len(read_page(journal, "gp").records) == 1


def test_missing_fts_tokenizer_falls_back_without_breaking_schema(tmp_path: Path) -> None:
    class NoFts(sqlite3.Connection):
        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
            if "CREATE VIRTUAL TABLE" in sql:
                message = "no such tokenizer: trigram"
                raise sqlite3.OperationalError(message)
            return super().execute(sql, *args)

    with closing(sqlite3.connect(":memory:", factory=NoFts)) as connection:
        JournalStore._create_fts(connection)
        assert not connection.execute("SELECT name FROM sqlite_master").fetchall()


def test_export_save_cancel_and_overwrite_behavior(tmp_path: Path) -> None:
    async def exercise() -> None:
        callbacks = []
        notices = []
        screen = SimpleNamespace(
            app=SimpleNamespace(push_screen=lambda dialog, callback: callbacks.append(callback)),
            notify=lambda text, **kwargs: notices.append(text),
        )
        record = {"user_sql": "-- exact\r\nselect 1", "submissions": []}
        export_record(screen, record, ".sql")
        callbacks.pop()(None)
        assert not callbacks
        export_record(screen, record, ".sql")
        callbacks.pop()("saved.sql")
        callbacks.pop()(None)
        assert not (tmp_path / "saved.sql").exists()
        for _ in range(2):
            export_record(screen, record, ".sql")
            callbacks.pop()("saved.sql")
            callbacks.pop()(tmp_path)
        assert (tmp_path / "saved.sql").read_bytes() == record["user_sql"].encode()
        assert "Exported" in notices[0]
        assert "failed" in notices[1]
        export_record(screen, record, ".json")
        callbacks.pop()("saved.json")
        callbacks.pop()(tmp_path)
        assert json.loads((tmp_path / "saved.json").read_text()) == record

    asyncio.run(exercise())


def test_schema_creation_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = QueryJournal(tmp_path)
    store = journal.store("gp")

    def fail(connection: sqlite3.Connection) -> None:
        message = "simulated schema write failure"
        raise sqlite3.DatabaseError(message)

    monkeypatch.setattr(store, "_create_fts", fail)
    with journal.action("gp", "gp", "user"):
        pass
    with closing(sqlite3.connect(str(store.path))) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not connection.execute("SELECT name FROM sqlite_master").fetchall()
    assert journal.take_warning()


def test_existing_fts_database_remains_searchable_if_match_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = QueryJournal(tmp_path)
    with journal.action("gp", "gp", "user", user_sql="select needle"):
        pass
    original = sqlite3.connect

    class WithoutMatch(sqlite3.Connection):
        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
            if " MATCH " in sql:
                message = "tokenizer unavailable"
                raise sqlite3.OperationalError(message)
            return super().execute(sql, *args)

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=WithoutMatch)

    monkeypatch.setattr(sqlite3, "connect", connect)
    assert len(read_page(journal, "gp", query="needle").records) == 1


def test_nested_action_reuses_connection_but_keeps_transactions_short(tmp_path: Path) -> None:
    journal = QueryJournal(tmp_path)
    with journal.action("gp", "gp", "user") as first:
        store = journal.store("gp")
        connection = store._local.connection
        assert not connection.in_transaction
        with journal.action("gp", "gp", "user") as second:
            assert store._local.connection is connection
            assert first.record["action_id"] != second.record["action_id"]
        assert not connection.in_transaction
    assert store._local.connection is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
