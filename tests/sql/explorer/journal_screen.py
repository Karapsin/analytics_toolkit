from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace
from typing import TYPE_CHECKING

from analytics_toolkit.sql.execution.observation import observed_connection
from analytics_toolkit.sql_explorer import journal_screen as module
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.file_commands import NewFileScreen
from analytics_toolkit.sql_explorer.journal import QueryJournal
from analytics_toolkit.sql_explorer.journal_reader import JournalPage
from analytics_toolkit.sql_explorer.journal_screen import JournalScreen
from textual.widgets import Button, Input, OptionList, Static, TextArea

from tests.sql._support.ui_workers import wait_for_ui_workers
from tests.sql.explorer.app import FakeSession
from tests.sql.explorer.journal import Driver

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_journal_shortcuts_share_ctrl_command_and_fn_parity(tmp_path: Path) -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.journal = QueryJournal(tmp_path)
        application = SqlExplorerApp(session)
        async with application.run_test(size=(130, 45)) as pilot:
            for key in ("ctrl+h", "meta+h", "super+h", "hyper+h"):
                for focus in (
                    application.active_workspace.editor,
                    application.active_workspace.command_input,
                ):
                    focus.focus()
                    await pilot.press(key)
                    await pilot.pause()
                    assert isinstance(application.screen, JournalScreen)
                    await pilot.press("escape")
                    assert not isinstance(application.screen, JournalScreen)
            await pilot.press("ctrl+h")
            await pilot.press("ctrl+h")
            assert not isinstance(application.screen, JournalScreen)

    asyncio.run(exercise())


def test_journal_filter_search_preview_and_open_preserve_editor(tmp_path: Path) -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.journal = QueryJournal(tmp_path)
        with session.journal.action(
            "gp", "gp", "user", user_sql="select * from public.orders", source_file="orders.sql"
        ):
            observed_connection(Driver()).execute("select * from public.orders LIMIT 201")
        with session.journal.action("gp", "gp", "background", context={"kind": "schema"}):
            observed_connection(Driver()).execute("SHOW SCHEMAS")
        application = SqlExplorerApp(session)
        async with application.run_test(size=(150, 45)) as pilot:
            original = application.active_workspace
            original.editor.text = "unsaved work"
            application._command_journal([])
            await pilot.pause()
            screen = application.screen
            assert isinstance(screen, JournalScreen)
            await wait_for_ui_workers(pilot, screen.workers)
            await pilot.pause()
            assert len(screen.records) == 1
            assert screen.query_one("#journal-sql", TextArea).text == "select * from public.orders"
            await pilot.click("#journal-next-sql")
            assert "LIMIT 201" in screen.query_one("#journal-sql", TextArea).text
            await pilot.click("#journal-filter")
            await wait_for_ui_workers(pilot, screen.workers)
            await pilot.pause()
            assert len(screen.records) == 2
            search = screen.query_one("#journal-search", Input)
            search.value = "orders"
            await pilot.pause()
            await wait_for_ui_workers(pilot, screen.workers)
            await pilot.pause()
            assert len(screen.records) == 1
            screen._receive_page(-1, JournalPage((), None, "stale"))
            assert len(screen.records) == 1
            await pilot.click("#journal-open")
            await pilot.pause()
            assert application.active_workspace is not original
            assert application.active_workspace.current_file is None
            assert application.active_workspace.editor.text == "select * from public.orders"
            assert application.active_workspace.saved_text == ""
            assert original.editor.text == "unsaved work"
            assert session.executed == []

    asyncio.run(exercise())


def test_empty_journal_command_and_internal_open(tmp_path: Path) -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.journal = QueryJournal(tmp_path)
        application = SqlExplorerApp(session)
        async with application.run_test(size=(130, 45)) as pilot:
            application._command_journal(["bad"])
            assert not isinstance(application.screen, JournalScreen)
            application._command_journal([])
            await pilot.pause()
            screen = application.screen
            assert isinstance(screen, JournalScreen)
            await wait_for_ui_workers(pilot, screen.workers)
            assert screen.query_one("#journal-open", Button).disabled
            assert screen.query_one("#journal-more", Button).disabled
            await pilot.press("ctrl+f")
            assert application.focused is screen.query_one("#journal-search")
            with session.journal.action("gp", "gp", "background"):
                observed_connection(Driver()).execute("SHOW SCHEMAS")
            await pilot.click("#journal-filter")
            await pilot.pause()
            await wait_for_ui_workers(pilot, screen.workers)
            await pilot.pause()
            screen.query_one("#journal-list", OptionList).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert application.active_workspace.editor.text == "SHOW SCHEMAS"
            assert session.executed == []

    asyncio.run(exercise())


def test_pages_replace_previous_records_and_export_opens_save_dialog(tmp_path: Path) -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.journal = QueryJournal(tmp_path)
        for index in range(105):
            with session.journal.action("gp", "gp", "user", user_sql=f"select {index}"):
                pass
        application = SqlExplorerApp(session)
        async with application.run_test(size=(150, 45)) as pilot:
            application.action_open_journal()
            await pilot.pause()
            screen = application.screen
            assert isinstance(screen, JournalScreen)
            await wait_for_ui_workers(pilot, screen.workers)
            first_ids = {record["action_id"] for record in screen.records}
            assert len(first_ids) == 100
            await pilot.click("#journal-more")
            await pilot.pause()
            await wait_for_ui_workers(pilot, screen.workers)
            await pilot.pause()
            assert len(screen.records) == 5
            assert not first_ids & {record["action_id"] for record in screen.records}
            assert screen.query_one("#journal-more", Button).disabled
            await pilot.click("#journal-previous")
            await pilot.pause()
            await wait_for_ui_workers(pilot, screen.workers)
            assert {record["action_id"] for record in screen.records} == first_ids
            await pilot.click("#journal-export-sql")
            assert isinstance(application.screen, NewFileScreen)
            assert application.screen.suffix == ".sql"
            await pilot.press("escape")
            await pilot.click("#journal-export-json")
            assert isinstance(application.screen, NewFileScreen)
            assert application.screen.suffix == ".json"
            await pilot.press("escape")
            await pilot.click("#journal-refresh")
            await pilot.pause()
            await wait_for_ui_workers(pilot, screen.workers)
            assert len(screen.records) == 100
            await pilot.click("#journal-close")
            assert not isinstance(application.screen, JournalScreen)

    asyncio.run(exercise())


def test_journal_notices_missing_details_and_busy_modal_guards(tmp_path: Path) -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.journal = QueryJournal(tmp_path)
        with session.journal.action("gp", "gp", "create_table", context={"table": "orders"}):
            pass
        application = SqlExplorerApp(session)
        async with application.run_test(size=(150, 45)) as pilot:
            application._exit_requested = True
            application.action_open_journal()
            assert not isinstance(application.screen, JournalScreen)
            application._exit_requested = False
            application.push_screen(
                NewFileScreen(suffix=".sql", title="Save", placeholder="query.sql")
            )
            application.action_open_journal()
            assert isinstance(application.screen, NewFileScreen)
            await pilot.press("escape")
            notices = []
            application.notify = lambda message, **kwargs: notices.append(message)
            session.journal.warn(OSError("disk full"))
            application._update_status()
            application._update_status()
            assert len(notices) == 1
            application.action_open_journal()
            await pilot.pause()
            screen = application.screen
            assert isinstance(screen, JournalScreen)
            await wait_for_ui_workers(pilot, screen.workers)
            assert screen.query_one("#journal-export-sql", Button).disabled
            assert not screen.query_one("#journal-export-json", Button).disabled
            screen._open()
            assert application.screen is screen
            screen._receive_record(-1, None, "stale")
            assert screen._record is not None
            screen._select_record("deleted")
            await pilot.pause()
            await wait_for_ui_workers(pilot, screen.workers)
            await pilot.pause()
            assert screen._record is None
            assert screen.query_one("#journal-export-json", Button).disabled

    asyncio.run(exercise())


def test_journal_ignores_nested_dialog_events_and_stale_exports(tmp_path: Path) -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.journal = QueryJournal(tmp_path)
        application = SqlExplorerApp(session)
        async with application.run_test(size=(150, 45)) as pilot:
            application.action_open_journal()
            await pilot.pause()
            screen = application.screen
            assert isinstance(screen, JournalScreen)
            await wait_for_ui_workers(pilot, screen.workers)
            generation = screen._generation
            screen.on_input_changed(Input.Changed(Input(id="new-file-name"), "export.sql"))
            screen.on_option_list_option_highlighted(
                SimpleNamespace(option_list=OptionList(id="nested"), option_index=0)
            )
            screen.on_option_list_option_selected(
                SimpleNamespace(option_list=OptionList(id="nested"))
            )
            screen.on_button_pressed(Button.Pressed(Button(id="new-file-cancel")))
            screen.on_button_pressed(
                Button.Pressed(screen.query_one("#journal-export-json", Button))
            )
            assert screen._generation == generation
            assert application.screen is screen
            with session.journal.action("gp", "gp", "user", user_sql="select 1") as action:
                pass
            record = session.journal.store("gp").load(action.record["action_id"])
            record["submissions"] = [None]
            record["error"] = {"type": "ValueError", "message": "query failed"}
            screen._show_record(record)
            assert screen.query_one("#journal-sql", TextArea).text == "select 1"
            assert "query failed" in screen.query_one("#journal-details", Static).renderable

    asyncio.run(exercise())


def test_cancelled_journal_workers_do_not_publish_late_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()

    def blocked_page(*args: object, **kwargs: object) -> JournalPage:
        entered.set()
        assert release.wait(3)
        return JournalPage((), None, "late response")

    async def exercise() -> None:
        session = FakeSession()
        session.journal = QueryJournal(tmp_path)
        application = SqlExplorerApp(session)
        async with application.run_test(size=(150, 45)) as pilot:
            monkeypatch.setattr(module, "read_page", blocked_page)
            application.action_open_journal()
            await pilot.pause()
            screen = application.screen
            assert isinstance(screen, JournalScreen)
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            screen.workers.cancel_all()
            release.set()
            await pilot.pause()
            assert "late response" not in str(
                screen.query_one("#journal-status", Static).renderable
            )
            assert not screen.records

    try:
        asyncio.run(exercise())
    finally:
        release.set()
