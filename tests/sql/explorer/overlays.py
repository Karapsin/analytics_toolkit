from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from analytics_toolkit.sql_explorer import app as app_module
from analytics_toolkit.sql_explorer.app import ResultTable, SqlEditor, SqlExplorerApp
from analytics_toolkit.sql_explorer.completion import CompletionRequest, CompletionResult
from analytics_toolkit.sql_explorer.widgets import (
    CompletionMenu,
    DiscardChangesScreen,
    FileNavigationScreen,
    FindReplaceBar,
)
from textual.widgets import Input

from tests.sql.explorer.app import FakeSession


def test_dynamic_binding_removal_supports_both_textual_storage_shapes() -> None:
    legacy = SimpleNamespace(keys={"f8": object()})
    modern = SimpleNamespace(key_to_bindings={"f8": [object()]})
    app_module._remove_dynamic_binding(legacy, "f8")
    app_module._remove_dynamic_binding(modern, "f8")
    assert legacy.keys == {}
    assert modern.key_to_bindings == {}


def test_search_panel_is_compact_right_overlay_with_responsive_limits() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(200, 40)):
            bar = application.query_one(FindReplaceBar)
            assert bar.styles.width is not None
            assert bar.styles.width.value == 12.5
            assert bar.styles.min_width is not None
            assert bar.styles.min_width.value == 24
            assert bar.styles.max_width is not None
            assert bar.styles.max_width.value == 48
            assert bar.styles.dock == "right"
            assert bar.styles.layer == "overlay"

    asyncio.run(exercise())


def test_visible_find_replace_panel_owns_vertical_arrows() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            application.action_find_next_control()
            application.disable_find_navigation()
            assert application.focused is editor
            await pilot.press("ctrl+f")
            application.enable_find_navigation()

            for control_id in (
                "find-next",
                "replace-pattern",
                "replace-current",
                "replace-all",
                "find-pattern",
            ):
                await pilot.press("down")
                assert application.focused.id == control_id

            editor.focus()
            await pilot.press("down")
            assert application.focused.id == "find-pattern"
            await pilot.press("up")
            assert application.focused.id == "replace-all"
            await pilot.press("escape")
            application.disable_find_navigation()
            assert application.focused is editor

    asyncio.run(exercise())


def test_confirmation_arrows_select_topmost_binary_dialog_actions() -> None:
    async def exercise() -> None:
        session = FakeSession()
        application = SqlExplorerApp(session)
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "delete from sample"
            await pilot.press("ctrl+f")
            await pilot.press("f5")

            assert application.focused.id == "confirm-execute"
            application.action_find_next_control()
            assert application.focused.id == "confirm-execute"
            await pilot.press("right")
            assert application.focused.id == "confirm-cancel"
            await pilot.press("left")
            assert application.focused.id == "confirm-execute"
            await pilot.press("right")
            await pilot.press("enter")
            await pilot.pause()
            assert len(application.screen_stack) == 1
            assert session.executed == []

            application.push_screen(DiscardChangesScreen(Path("next.sql")))
            await pilot.pause()
            assert application.focused.id == "discard-confirm"
            await pilot.press("right")
            assert application.focused.id == "discard-cancel"
            await pilot.press("left")
            assert application.focused.id == "discard-confirm"
            await pilot.press("right")
            await pilot.press("enter")
            await pilot.pause()
            assert len(application.screen_stack) == 1

    asyncio.run(exercise())


def test_escape_toggles_editor_command_and_skips_results() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            command = application.query_one("#command-input", Input)
            application.show_dataframe(pd.DataFrame({"x": [1]}))
            table = application.query_one(ResultTable)

            editor.focus()
            await pilot.press("escape")
            assert application.focused is command
            await pilot.press("escape")
            assert application.focused is editor
            await pilot.press("down")
            assert application.focused is table
            await pilot.press("escape")
            assert application.focused is command
            await pilot.press("escape")
            assert application.focused is editor

    asyncio.run(exercise())


def test_escape_overlays_consume_it_first() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)

            await pilot.press("ctrl+f")
            bar = application.query_one(FindReplaceBar)
            assert bar.styles.display == "block"
            application.action_escape()
            await pilot.pause()
            assert bar.styles.display == "none"
            assert application.focused is editor

            editor.text = "L"
            editor.cursor_location = (0, 1)
            await pilot.press("tab")
            menu = application.query_one(CompletionMenu)
            assert menu.is_open is True
            stopped: list[bool] = []
            application.on_option_list_option_selected(
                SimpleNamespace(option_list=menu, stop=lambda: stopped.append(True))
            )
            assert stopped == [True]
            assert editor.text == "left join"

            editor.text = "L"
            editor.cursor_location = (0, 1)
            await pilot.press("tab")
            application.action_escape()
            await pilot.pause()
            assert menu.is_open is False
            assert application.focused is editor

            menu.open(())
            menu.move_highlight(1)
            assert menu.is_open is False
            assert menu.selected_suggestion() is None

            application.action_open_navigation()
            assert isinstance(application.screen, FileNavigationScreen)
            application.action_escape()
            application.action_open_navigation()
            assert isinstance(application.screen, FileNavigationScreen)
            await pilot.press("escape")

    asyncio.run(exercise())


def test_terminal_forwarded_command_o_alias_opens_navigation_mode() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            await pilot.press("meta+o")
            assert isinstance(application.screen, FileNavigationScreen)
            application._set_notice("Navigation remains active.")
            application._update_status()
            request = CompletionRequest("gp", "gp", "keyword", "s")
            result = CompletionResult(request, 1, ())
            application._receive_completion(result)
            application._receive_namespace(result)
            await asyncio.sleep(1.1)
            await pilot.pause()
            workspace = application.screen_stack[0]
            assert "Navigation remains active." in str(workspace.query_one("#notice").render())
            await pilot.press("escape")

    asyncio.run(exercise())
