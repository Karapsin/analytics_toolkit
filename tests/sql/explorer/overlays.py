from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pandas as pd
from analytics_toolkit.sql_explorer import app as app_module
from analytics_toolkit.sql_explorer.app import ResultTable, SqlEditor, SqlExplorerApp
from analytics_toolkit.sql_explorer.widgets import (
    CompletionMenu,
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


def test_escape_cycles_panes_and_overlays_consume_it_first() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            command = application.query_one("#command-input", Input)
            application.show_dataframe(pd.DataFrame({"x": [1]}))
            table = application.query_one(ResultTable)

            editor.focus()
            await pilot.press("escape")
            assert application.focused is table
            await pilot.press("escape")
            assert application.focused is command
            await pilot.press("escape")
            assert application.focused is editor

            await pilot.press("ctrl+f")
            bar = application.query_one(FindReplaceBar)
            assert bar.styles.display == "block"
            application.action_escape()
            await pilot.pause()
            assert bar.styles.display == "none"
            assert application.focused is editor

            editor.text = "SEL"
            editor.cursor_location = (0, 3)
            await pilot.press("tab")
            menu = application.query_one(CompletionMenu)
            assert menu.is_open is True
            application.action_escape()
            await pilot.pause()
            assert menu.is_open is False
            assert application.focused is editor

            menu.open(())
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
            await pilot.press("escape")

    asyncio.run(exercise())
