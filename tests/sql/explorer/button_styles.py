from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.create_table_screen import CreateTableScreen
from analytics_toolkit.sql_explorer.exports import ConfirmExportScreen
from analytics_toolkit.sql_explorer.file_commands import NewSqlFileScreen
from analytics_toolkit.sql_explorer.statements import build_execution_plan
from analytics_toolkit.sql_explorer.tabs import SaveChangesScreen
from analytics_toolkit.sql_explorer.widgets import (
    ConfirmMutationScreen,
    DiscardChangesScreen,
    FileNavigationScreen,
)
from textual.color import Color
from textual.widgets import Button, Collapsible, Input

from tests.sql._support.explorer import FakeSession


@pytest.mark.parametrize(
    "screen_name", ["save", "mutation", "discard", "export", "new-file", "directory", "find"]
)
def test_dialogs_share_picker_hover_and_amber_keyboard_focus(screen_name: str) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(120, 40)) as pilot:
            screens = {
                "save": lambda: SaveChangesScreen("query.sql"),
                "mutation": lambda: ConfirmMutationScreen(
                    build_execution_plan("delete from t", "gp"), db_key="gp", backend="gp"
                ),
                "discard": lambda: DiscardChangesScreen(Path("query.sql")),
                "export": lambda: ConfirmExportScreen("Save results?", confirm_label="Save all"),
                "new-file": NewSqlFileScreen,
                "directory": lambda: FileNavigationScreen(Path.cwd(), select_directory=True),
            }
            if screen_name == "find":
                application.action_open_find()
                container = application.query_one("#find-replace-bar")
            else:
                container = screens[screen_name]()
                application.push_screen(container)
            await pilot.pause()
            buttons = list(container.query(Button))
            neutral = Color.parse(application.get_css_variables()["surface"])
            amber = Color.parse(application.get_css_variables()["accent"])
            hover = Color.parse(application.get_css_variables()["panel-lighten-2"])
            for selected in buttons:
                await pilot.hover(offset=(0, 0))
                selected.focus()
                await pilot.pause()
                for button in buttons:
                    assert button.styles.background == (amber if button is selected else neutral)
                    assert not button.styles.text_style.reverse
                    if button is not selected:
                        await pilot.hover(f"#{button.id}")
                        assert button.styles.background == hover
                        assert selected.styles.background == amber
                        await pilot.hover(offset=(0, 0))
            await pilot.hover(offset=(0, 0))
            for field in container.query(Input):
                field.focus()
                await pilot.pause()
                assert all(button.styles.background == neutral for button in buttons)

    asyncio.run(exercise())


@pytest.mark.parametrize("advanced", [False, True])
def test_create_table_buttons_keep_individual_focus_and_hover_colors(advanced: bool) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(208, 47)) as pilot:
            screen = CreateTableScreen(
                "gp",
                "gp",
                {
                    "table_name": "analytics.daily_orders",
                    "rows": [("customer_id", "BIGINT"), ("revenue", "NUMERIC(18,2)")],
                },
            )
            application.push_screen(screen)
            await pilot.pause()
            screen.query_one("#create-table-advanced", Collapsible).collapsed = not advanced
            await pilot.pause()
            field = screen.query_one("#create-table-name", Input)
            buttons = list(screen.query(Button))
            removals = list(screen.query(".remove-column", Button))
            assert len(removals) == 2
            assert len(buttons) == 5
            palette = application.get_css_variables()
            neutral = Color.parse(palette["surface"])
            hover = Color.parse(palette["panel-lighten-2"])
            amber = Color.parse(palette["accent"])

            def assert_removals_readable() -> None:
                for removal in removals:
                    assert removal.region.width == 3
                    assert removal.region.height == 1
                    assert "x" in removal.render_line(0).text
                    assert removal.tooltip == "Remove column"

            for button in buttons:
                await pilot.hover(offset=(0, 0))
                field.focus()
                await pilot.pause()
                assert all(item.styles.background == neutral for item in buttons)
                assert_removals_readable()
                await pilot.hover(button)
                assert_removals_readable()
                assert button.styles.background == hover
                button.focus()
                await pilot.pause()
                for item in buttons:
                    assert item.styles.background == (amber if item is button else neutral)
                assert_removals_readable()
                assert button.styles.color == Color.parse(palette["background"])
                await pilot.hover(offset=(0, 0))
                assert button.styles.background == amber
                assert_removals_readable()
                field.focus()
                await pilot.pause()
                assert all(item.styles.background == neutral for item in buttons)
                assert_removals_readable()
                button.disabled = True
                await pilot.pause()
                assert button.styles.background == neutral
                await pilot.hover(button)
                assert button.styles.background == neutral
                assert button.styles.color == Color.parse(palette["text-disabled"])
                assert_removals_readable()
                await pilot.hover(offset=(0, 0))
                button.disabled = False
                await pilot.pause()
                assert button.styles.background == neutral
                assert_removals_readable()

    asyncio.run(exercise())
