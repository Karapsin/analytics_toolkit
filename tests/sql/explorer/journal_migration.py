from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit.sql_explorer import journal_store
from analytics_toolkit.sql_explorer.journal import QueryJournal
from analytics_toolkit.sql_explorer.journal_exports import export_text
from analytics_toolkit.sql_explorer.journal_reader import read_page, read_record

if TYPE_CHECKING:
    from pathlib import Path


def test_v1_migration_preserves_history_and_backfills_feed(tmp_path: Path) -> None:
    journal = QueryJournal(tmp_path)
    with journal.action(
        "gp", "gp", "user", user_sql="-- preserved\nselect * from sales.orders"
    ) as action:
        pass
    before = read_record(journal, "gp", action.record["action_id"])
    path = journal.store("gp").path
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE completion_events")
        connection.execute("PRAGMA user_version=1")
    upgraded = QueryJournal(tmp_path)
    assert read_record(upgraded, "gp", action.record["action_id"]) == before
    assert read_page(upgraded, "gp", query="sales.orders").records
    assert export_text(before, ".sql") == before["user_sql"]
    events = upgraded.store("gp").completed_since(0)
    assert [event["action_id"] for event in events] == [action.record["action_id"]]
    assert "user_sql" not in events[0]
    with upgraded.action("gp", "gp", "user", user_sql="select 2"):
        pass
    assert len(QueryJournal(tmp_path).store("gp").completed_since(0)) == 2


def test_feed_excludes_failed_cancelled_internal_and_running_actions(tmp_path: Path) -> None:
    journal = QueryJournal(tmp_path)
    for origin in ("metadata", "background", "cancel"):
        with journal.action("gp", "gp", origin, user_sql="select 1"):
            pass
    message = "failed"
    with pytest.raises(ValueError, match="failed"), journal.action(
        "gp", "gp", "user", user_sql="bad SQL"
    ):
        raise ValueError(message)
    with journal.action("gp", "gp", "user", user_sql="select 2") as cancelled:
        cancelled.record["cancelled"] = True
    with journal.action("gp", "gp", "user", user_sql="select 3") as outer:
        with journal.action("gp", "gp", "export", user_sql="select 4") as inner:
            pass
        events = journal.store("gp").completed_since(0)
        assert [item["action_id"] for item in events] == [inner.record["action_id"]]
    events = journal.store("gp").completed_since(events[-1]["sequence"])
    assert [item["action_id"] for item in events] == [outer.record["action_id"]]


def test_migration_failure_rolls_back_without_destroying_v1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = QueryJournal(tmp_path)
    with journal.action("gp", "gp", "user", user_sql="select 1"):
        pass
    path = journal.store("gp").path
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE completion_events")
        connection.execute("PRAGMA user_version=1")

    def broken(connection):
        connection.execute("CREATE TABLE incomplete (id INTEGER)")
        message = "migration failed"
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(journal_store, "migrate_completion_events", broken)
    assert "migration failed" in read_page(QueryJournal(tmp_path), "gp").warning
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='incomplete'"
        ).fetchall()
