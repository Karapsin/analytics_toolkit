from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
from textual.widgets import Button, Collapsible, Input, Static

from tests.sql._support.explorer import FakeSession


async def _exercise_dialog_buttons(pilot: Any, container: Any, palette: dict[str, str]) -> None:
    buttons = list(container.query(Button))
    neutral = Color.parse(palette["surface"])
    amber = Color.parse(palette["accent"])
    hover = Color.parse(palette["panel-lighten-2"])
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


def _assert_removals_readable(removals: list[Button]) -> None:
    for removal in removals:
        assert removal.region.width == 3
        assert removal.region.height == 1
        assert "x" in removal.render_line(0).text
        assert removal.tooltip == "Remove column"


async def _hover_button(pilot: Any, button: Button) -> None:
    button.scroll_visible(animate=False)
    await pilot.pause()
    # Coordinates identify repeated removal controls on Textual 0.73 too.
    region = button.content_region
    x, y = region.x + region.width // 2, region.y
    await pilot.hover(offset=(x, y))
    target, _ = button.screen.get_widget_at(x, y)
    assert target is button


class DisabledColorReference(Static):
    """Resolve the expected CSS contract beside the subject, independently."""

    DEFAULT_CSS = """
    DisabledColorReference {
        color: $text-disabled;
        background: $surface;
        text-style: none;
        tint: transparent;
        display: none;
    }
    """


def _assert_disabled_color(button: Button, reference: DisabledColorReference) -> None:
    assert button.styles.color == reference.styles.color
    assert button.styles.auto_color == reference.styles.auto_color
    assert button.rich_style.color == reference.rich_style.color


async def _exercise_create_button(
    pilot: Any, screen: CreateTableScreen, button: Button, palette: dict[str, str]
) -> None:
    field = screen.query_one("#create-table-name", Input)
    buttons = list(screen.query(Button))
    removals = [item for item in buttons if item.has_class("remove-column")]
    neutral = Color.parse(palette["surface"])
    hover = Color.parse(palette["panel-lighten-2"])
    amber = Color.parse(palette["accent"])
    await pilot.hover(offset=(0, 0))
    field.focus()
    await pilot.pause()
    assert all(item.styles.background == neutral for item in buttons)
    _assert_removals_readable(removals)
    await _hover_button(pilot, button)
    _assert_removals_readable(removals)
    assert button.styles.background == hover
    button.focus()
    await pilot.pause()
    for item in buttons:
        assert item.styles.background == (amber if item is button else neutral)
    _assert_removals_readable(removals)
    assert button.styles.color == Color.parse(palette["background"])
    await pilot.hover(offset=(0, 0))
    assert button.styles.background == amber
    _assert_removals_readable(removals)
    await _exercise_disabled_button(pilot, screen, button, palette)


async def _exercise_disabled_button(
    pilot: Any, screen: CreateTableScreen, button: Button, palette: dict[str, str]
) -> None:
    field = screen.query_one("#create-table-name", Input)
    buttons = list(screen.query(Button))
    removals = [item for item in buttons if item.has_class("remove-column")]
    neutral = Color.parse(palette["surface"])
    field.focus()
    await pilot.pause()
    assert all(item.styles.background == neutral for item in buttons)
    _assert_removals_readable(removals)
    button.disabled = True
    await pilot.pause()
    assert button.styles.background == neutral
    await _hover_button(pilot, button)
    assert button.styles.background == neutral
    reference = DisabledColorReference()
    parent = button.parent
    assert parent is not None
    await parent.mount(reference, after=button)
    try:
        await pilot.pause()
        assert reference.parent is button.parent
        _assert_disabled_color(button, reference)
        # An independent, incorrect foreground must still fail the strict contract.
        reference.styles.color = "#ff0000"
        await pilot.pause()
        assert reference.rich_style.color != button.rich_style.color
        with pytest.raises(AssertionError):
            _assert_disabled_color(button, reference)
    finally:
        await reference.remove()
    _assert_removals_readable(removals)
    await pilot.hover(offset=(0, 0))
    button.disabled = False
    await pilot.pause()
    assert button.styles.background == neutral
    _assert_removals_readable(removals)


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
            await _exercise_dialog_buttons(pilot, container, application.get_css_variables())

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
            buttons = list(screen.query(Button))
            removals = [button for button in buttons if button.has_class("remove-column")]
            assert len(removals) == 2
            assert len(buttons) == 5
            palette = application.get_css_variables()
            for button in buttons:
                await _exercise_create_button(pilot, screen, button, palette)

    asyncio.run(exercise())


def test_disabled_color_comparison_rejects_only_rendered_foreground_mismatch() -> None:
    # Matching CSS tokens alone cannot establish the rendered color contract.
    button: Any = SimpleNamespace(
        styles=SimpleNamespace(color="disabled", auto_color=True),
        rich_style=SimpleNamespace(color="#707274"),
    )
    reference: Any = SimpleNamespace(
        styles=SimpleNamespace(color="disabled", auto_color=True),
        rich_style=SimpleNamespace(color="#6f7172"),
    )
    with pytest.raises(AssertionError):
        _assert_disabled_color(button, reference)
