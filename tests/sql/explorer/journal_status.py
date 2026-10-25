from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit.sql_explorer import journal_screen as module
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.journal import QueryJournal
from analytics_toolkit.sql_explorer.journal_reader import JournalPage, read_page
from analytics_toolkit.sql_explorer.journal_screen import JournalScreen
from textual.widgets import Button, Static

from tests.sql._support.explorer import FakeSession
from tests.sql._support.ui_workers import wait_for_ui_workers

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("warning", ["", "Cannot read journal: file is not a database"])
def test_empty_read_status_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warning: str
) -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.journal = QueryJournal(tmp_path)
        page = JournalPage((), None, warning)
        monkeypatch.setattr(module, "read_page", lambda *_args, **_kwargs: page)
        app = SqlExplorerApp(session)
        async with app.run_test(size=(130, 45)) as pilot:
            app.action_open_journal()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, JournalScreen)
            await wait_for_ui_workers(pilot, screen.workers)
            status = screen.query_one("#journal-status", Static)
            assert str(status.renderable) == (warning or "No matching journal entries.")
            if warning:
                assert "No matching journal entries" not in str(status.renderable)
            for name in ("open", "more", "export-sql", "export-json", "next-sql"):
                assert screen.query_one(f"#journal-{name}", Button).disabled
            assert not screen.records
            assert not screen.query_one("#journal-refresh", Button).disabled
            assert not screen.query_one("#journal-close", Button).disabled

            page = JournalPage((), None, "")
            screen.reload()
            await wait_for_ui_workers(pilot, screen.workers)
            assert str(status.renderable) == "No matching journal entries."
            screen._receive_page(-1, JournalPage((), None, "stale failure"))
            assert str(status.renderable) == "No matching journal entries."

            with session.journal.action("gp", "gp", "user", user_sql="select 1"):
                pass
            populated = read_page(session.journal, "gp")
            assert len(populated.records) == 1
            page = JournalPage(populated.records, populated.cursor, "partial warning")
            screen.reload()
            await pilot.pause()
            await wait_for_ui_workers(pilot, screen.workers)
            assert str(status.renderable) == "Page 1 · 1 entries. partial warning"
            assert len(screen.records) == 1
            assert not screen.query_one("#journal-open", Button).disabled
            assert session.executed == []

    asyncio.run(exercise())
