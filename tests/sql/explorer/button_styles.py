from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
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
from textual.widgets import Button, Input

from tests.sql.explorer.app import FakeSession


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
