from __future__ import annotations

import asyncio

import pytest
from analytics_toolkit.sql_explorer.app import ConfirmMutationScreen, SqlExplorerApp
from analytics_toolkit.sql_explorer.panes import CommandInput
from textual.widgets import Button, Input, Static

from tests.sql.explorer.app import FakeSession


@pytest.mark.parametrize("trigger", ["button", "shortcut"])
def test_run_executes_editor_selection_and_keeps_command_focus(trigger: str) -> None:
    async def exercise() -> None:
        session = FakeSession()
        application = SqlExplorerApp(session)
        async with application.run_test(size=(120, 40)) as pilot:
            workspace = application.active_workspace
            workspace.editor.text = "select 1;\nselect 2"
            workspace.editor.move_cursor((1, 0))
            workspace.editor.move_cursor((1, 8), select=True)
            if trigger == "button":
                await pilot.click("#run-query")
            else:
                await pilot.press("ctrl+enter")
            await pilot.pause()
            assert len(session.executed) == 1
            assert session.executed[0].statements == ("select 2",)
            if trigger == "button":
                assert application.focused is workspace.command_input
            assert workspace.query_one("#run-query", Button).display
            assert not workspace.query_one("#interrupt", Button).display

    asyncio.run(exercise())


def test_run_preserves_mutation_confirmation_focus() -> None:
    async def exercise() -> None:
        session = FakeSession()
        application = SqlExplorerApp(session)
        async with application.run_test(size=(120, 40)) as pilot:
            application.active_workspace.editor.text = "delete from sample"
            await pilot.click("#run-query")
            assert isinstance(application.screen, ConfirmMutationScreen)
            assert application.focused in application.screen.query(Button)
            assert not session.executed
            await pilot.press("n")
            assert application.active_workspace.query_one("#run-query", Button).display

    asyncio.run(exercise())


def test_run_visibility_is_per_tab_and_waits_for_worker_acknowledgement() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(120, 40)) as pilot:
            first = application.active_workspace
            run = first.query_one("#run-query", Button)
            stop = first.query_one("#interrupt", Button)
            for state in ("queued", "running", "cancelling"):
                first.query_state = state
                first.busy = state != "queued"
                first.cancelling = state == "cancelling"
                application._update_status(first)
                assert not run.display
                assert run.disabled
                assert stop.display == (state != "queued")
                assert stop.disabled == (state != "running")
            await pilot.press("ctrl+t")
            second = application.active_workspace
            assert second is not first
            assert second.query_one("#run-query", Button).display
            assert not run.display
            first.reset_query_state()
            application._update_status(first)
            assert run.display
            assert not stop.display
            first.busy = True
            application._update_status(first)
            assert not run.display
            first.reset_query_state()

    asyncio.run(exercise())


def test_entire_command_surface_focuses_input_without_changing_text() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(120, 40)) as pilot:
            workspace = application.active_workspace
            workspace.command_input.value = "help"
            application._set_notice("Command notice")
            panel = workspace.query_one(".command-panel")
            await pilot.pause()
            targets = [
                (".command-panel", (0, 0)),
                (".command-panel", (panel.region.width - 1, 2)),
                (".command-panel", (4, panel.region.height - 1)),
                ("#notice", (2, 1)),
                ("#command-row", (1, 0)),
            ]
            for selector, offset in targets:
                workspace.editor.focus()
                await pilot.pause()
                await pilot.click(selector, offset=offset)
                assert application.focused is workspace.command_input, (selector, offset)
                assert workspace.command_input.value == "help"
            workspace.query_state = "cancelling"
            workspace.busy = True
            workspace.cancelling = True
            application._update_status(workspace)
            workspace.editor.focus()
            await pilot.pause()
            await pilot.click("#interrupt")
            assert application.focused is workspace.command_input
            workspace.reset_query_state()
            workspace.command_input.cursor_position = 2
            await pilot.pause()
            await pilot.click("#notice")
            command = workspace.query_one("#command-input", CommandInput)
            assert command.cursor_position == 2
            assert command.has_focus
            command.focus_preserving_cursor()
            await pilot.pause()
            assert command.cursor_position == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("close", ["icon", "escape"])
def test_find_close_clears_own_notice_and_ignores_hidden_changes(close: str) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(120, 40)) as pilot:
            workspace = application.active_workspace
            notice = workspace.query_one("#notice", Static)
            find = workspace.query_one("#find-pattern", Input)
            assert "Enter text to find" not in str(notice.renderable)
            workspace.editor.text = "alpha alpha"
            await pilot.press("ctrl+f")
            assert "Enter text to find" in str(notice.renderable)
            find.value = "alpha"
            await pilot.pause()
            assert workspace.editor.search_match_count == 2
            if close == "icon":
                await pilot.click("#close-find")
            else:
                await pilot.press("escape")
            assert not workspace.find_bar.is_open
            assert application.focused is workspace.editor
            assert workspace.editor.search_match_count == 0
            assert str(notice.renderable) == ""
            find.value = "missing"
            workspace.find_bar.find_next()
            await pilot.pause()
            assert str(notice.renderable) == ""
            assert workspace.editor.search_match_count == 0
            await pilot.press("ctrl+f")
            assert find.value == "missing"
            assert "No matches" in str(notice.renderable)
            application._set_notice("Newer query notice")
            await pilot.click("#close-find")
            assert str(notice.renderable) == "Newer query notice"

    asyncio.run(exercise())


def test_browser_tab_shortcuts_wrap_and_restore_each_tabs_focus() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(120, 40)) as pilot:
            first = application.active_workspace
            first.command_input.focus()
            await pilot.press("ctrl+t")
            second = application.active_workspace
            second.editor.text = "select 2"
            await pilot.press("ctrl+t")
            third = application.active_workspace
            await pilot.press("ctrl+tab")
            assert application.active_workspace is first
            assert application.focused is first.command_input
            await pilot.press("ctrl+tab")
            assert application.active_workspace is second
            assert application.focused is second.editor
            assert second.editor.text == "select 2"
            await pilot.press("ctrl+shift+tab", "ctrl+shift+tab")
            assert application.active_workspace is third
            third.editor.text = "delete from sample"
            await pilot.press("ctrl+enter")
            assert isinstance(application.screen, ConfirmMutationScreen)
            await pilot.press("ctrl+tab", "ctrl+shift+tab")
            assert application.active_workspace is third
            assert isinstance(application.screen, ConfirmMutationScreen)
            await pilot.press("n")

    asyncio.run(exercise())


def test_editor_surface_focus_preserves_selection_and_search_controls() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(120, 40)) as pilot:
            workspace = application.active_workspace
            editor = workspace.editor
            editor.text = "select alpha from sample"
            editor.move_cursor((0, 7))
            editor.move_cursor((0, 12), select=True)
            pane = workspace.query_one(".query-pane")
            await pilot.pause()
            for selector, offset in (
                (".query-pane", (0, 0)),
                (".query-pane", (pane.region.width - 1, 4)),
                (".query-pane", (4, pane.region.height - 1)),
                ("#editor-status", (0, 0)),
                ("#editor-status", (4, 1)),
            ):
                workspace.command_input.focus()
                await pilot.pause()
                await pilot.click(selector, offset=offset)
                assert application.focused is editor
                assert editor.selected_text == "alpha"
                assert editor.cursor_location == (0, 12)
            await pilot.press("ctrl+f")
            await pilot.click("#replace-pattern")
            assert application.focused is workspace.query_one("#replace-pattern", Input)
            await pilot.click("#close-find")
            assert application.focused is editor
            assert not workspace.find_bar.is_open

    asyncio.run(exercise())
