from __future__ import annotations

import asyncio

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.create_table_screen import BooleanCheckbox, CreateTableScreen
from analytics_toolkit.sql_explorer.help_text import MOVEMENT_HELP, SHORTCUTS_HELP, help_table
from rich.console import Console
from textual.color import Color
from textual.geometry import Region
from textual.widgets import Button, Collapsible, Input, OptionList, Select

from tests.sql._support.explorer import FakeSession


def _assert_checkbox_painted(app: SqlExplorerApp, checkbox: BooleanCheckbox) -> None:
    region = checkbox.region
    local = checkbox.render_lines(Region(0, 0, region.width, 1))[0]
    composed = app.screen._compositor.render_strips()[region.y].crop(region.x, region.right)
    expected = "[x] " if checkbox.value else "[ ] "
    for strip in (local, composed):
        assert strip.text.startswith(expected)
        assert strip.text.rstrip().endswith(str(checkbox.value))
        assert all(
            segment.style.color.get_truecolor().hex
            != segment.style.bgcolor.get_truecolor().hex
            for segment in strip.crop(0, 3)
        )


def _assert_removal_painted(app: SqlExplorerApp, remove: Button) -> None:
    region = remove.region
    local = remove.render_lines(Region(0, 0, region.width, 1))[0]
    composed = app.screen._compositor.render_strips()[region.y].crop(region.x, region.right)
    for strip in (local, composed):
        assert strip.text.strip() == "x"
        assert strip.cell_length == 3
    assert remove.content_region == remove.region


def test_boolean_marks_labels_and_removal_control_agree() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 45)) as pilot:
            screen = CreateTableScreen(
                "gp",
                "gp",
                {
                    "table_name": "sandbox.orders",
                    "rows": [("id", "BIGINT"), ("value", "NUMERIC")],
                },
            )
            await app.push_screen(screen)
            await pilot.pause()

            for checkbox in screen.query(BooleanCheckbox):
                _assert_checkbox_painted(app, checkbox)
                assert str(checkbox.label).endswith("False")
            skip = screen.query_one("#create-skip_if_exists", BooleanCheckbox)
            drop = screen.query_one("#create-drop_if_exists", BooleanCheckbox)
            skip.focus()
            await pilot.pause()
            _assert_checkbox_painted(app, skip)
            await pilot.press("space")
            await pilot.pause()
            assert skip.value
            _assert_checkbox_painted(app, skip)
            assert str(skip.label).endswith("True")
            await pilot.click("#create-drop_if_exists")
            await pilot.pause()
            assert drop.value and not skip.value
            _assert_checkbox_painted(app, skip)
            _assert_checkbox_painted(app, drop)
            skip.disabled = True
            await pilot.pause()
            _assert_checkbox_painted(app, skip)
            await pilot.click("#create-skip_if_exists")
            await pilot.pause()
            assert not skip.value
            _assert_checkbox_painted(app, skip)
            skip.disabled = False
            remove = screen.query(".remove-column").first(Button)
            assert str(remove.label) == "x"
            assert remove.tooltip == "Remove column"
            for state in ("neutral", "hover", "focus"):
                screen.query_one("#create-table-name", Input).focus()
                await pilot.hover(offset=(0, 0))
                if state != "neutral":
                    await pilot.hover(offset=(remove.region.x + 1, remove.region.y))
                if state == "focus":
                    remove.focus()
                await pilot.pause()
                _assert_removal_painted(app, remove)
            remove.press()
            await pilot.pause()
            assert screen.values()["rows"] == [("value", "NUMERIC")]

    asyncio.run(exercise())


def test_type_menu_follows_active_input_and_resize_and_closes_on_focus_change() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 45)) as pilot:
            screen = CreateTableScreen(
                "gp",
                "gp",
                {
                    "table_name": "sandbox.orders",
                    "rows": [("id", "BIGINT"), ("value", "NUMERIC")],
                },
            )
            await app.push_screen(screen)
            field = screen.query(".create-column-type").first(Input)
            field.focus()
            field.value = "B"
            await pilot.pause()
            screen._position_types()
            await pilot.pause()
            menu = screen.query_one("#create-type-options", OptionList)
            assert menu.display
            assert menu.region.x == field.region.x
            assert menu.region.y == field.region.bottom
            assert menu.region.width == field.region.width
            await pilot.resize_terminal(100, 40)
            await pilot.pause()
            screen._position_types()
            await pilot.pause()
            assert menu.region.x == field.region.x
            assert menu.region.y == field.region.bottom
            screen.action_form_vertical(1)
            screen.action_form_enter()
            await pilot.pause()
            assert field.value in {"BIGINT", "BOOLEAN"}
            screen.query_one("#create-table-name", Input).focus()
            await pilot.pause()
            assert not menu.display

    asyncio.run(exercise())


def test_help_wraps_descriptions_beside_commands_without_interpreting_brackets() -> None:
    for content in (MOVEMENT_HELP, SHORTCUTS_HELP):
        console = Console(width=70, color_system=None)
        with console.capture() as capture:
            table = help_table(content)
            console.print(table)
        rendered = capture.get()
        assert all(
            len(line) - len(line.lstrip()) >= 27
            for line in rendered.splitlines()
            if line.strip() in {"command", "D", "editor/command", "disabled)"}
        )
        if content == MOVEMENT_HELP:
            assert "mv [A [B]]" in rendered
            assert any(cell.plain == "mv [A [B]]" for cell in table.columns[0]._cells)
            assert any("move to row A" in cell.plain for cell in table.columns[1]._cells)


def _assert_selector_painted(app: SqlExplorerApp, selector: Select, expected: str) -> None:
    frame = selector.query_one("SelectCurrent")
    assert selector.content_region == selector.region
    assert selector.region.height == 3
    assert frame.region == selector.content_region
    label = selector.query_one("SelectCurrent > Static#label")
    arrow = selector.query_one(
        "SelectCurrent > .up-arrow" if selector.expanded else "SelectCurrent > .down-arrow"
    )
    composed = app.screen._compositor.render_strips()
    for child, text in ((label, expected), (arrow, "▲" if selector.expanded else "▼")):
        region = child.region
        assert region.width > 0 and region.height == 1
        ancestor = child.parent
        while ancestor is not None:
            clip = ancestor.scrollable_content_region
            assert clip.x <= region.x < region.right <= clip.right
            assert clip.y <= region.y < region.bottom <= clip.bottom
            ancestor = ancestor.parent
        row = composed[region.y].crop(region.x, region.right)
        assert text in row.text
        assert text in "".join(
            segment.text
            for segment in row
            if segment.style
            and not segment.style.conceal
            and segment.style.color != segment.style.bgcolor
        )


@pytest.mark.parametrize(
    "source,invalid", [("table_schema", False), ("table_schema", True), ("from_sql", False)]
)
def test_source_selector_composes_inside_frame_through_focus_resize_and_scroll(
    source: str, invalid: bool
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(208, 47)) as pilot:
            screen = CreateTableScreen(
                "gp",
                "gp",
                {
                    "table_name": "sandbox.orders",
                    "source": source,
                    "from_sql": "select 1",
                    "rows": [("id", "" if invalid else "BIGINT"), ("value", "NUMERIC")],
                },
            )
            await app.push_screen(screen)
            await pilot.pause()
            selector = screen.query_one("#create-table-source", Select)
            if invalid:
                screen.query_one("#create-table-submit", Button).press()
                await pilot.pause()
                notice = screen.query_one("#create-table-notice")
                assert "Each column needs" in str(notice.renderable)
                assert app.session.executed == []
            _assert_selector_painted(app, selector, source)
            frame = selector.query_one("SelectCurrent")
            neutral = Color.parse(app.get_css_variables()["panel-lighten-3"])
            assert frame.styles.border_top[1] == neutral
            selector.focus()
            await pilot.pause()
            _assert_selector_painted(app, selector, source)
            assert frame.styles.border_top[1] == Color.parse(app.get_css_variables()["accent"])
            await pilot.resize_terminal(110, 40)
            await pilot.pause()
            _assert_selector_painted(app, selector, source)
            fields = screen.query_one("#create-table-fields")
            screen.query_one(Collapsible).collapsed = False
            await pilot.pause()
            fields.scroll_end(animate=False)
            await pilot.pause()
            assert fields.scroll_y > 0
            selector.scroll_visible(animate=False)
            await pilot.pause()
            _assert_selector_painted(app, selector, source)

    asyncio.run(exercise())


def test_advanced_boolean_selectors_keep_labels_and_native_keyboard_choices() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(208, 47)) as pilot:
            screen = CreateTableScreen("ch", "ch", {"table_name": "sandbox.orders"})
            await app.push_screen(screen)
            screen.query_one(Collapsible).collapsed = False
            await pilot.pause()
            choices = [
                selector
                for selector in screen.query(Select)
                if selector.has_class("create-advanced-field")
            ]
            assert choices
            for selector in choices:
                selector.focus()
                selector.scroll_visible(animate=False)
                await pilot.pause()
                _assert_selector_painted(app, selector, "Default")
                await pilot.press("right")
                await pilot.pause()
                assert selector.value == "True"
                _assert_selector_painted(app, selector, "True")
                await pilot.press("left")
                await pilot.pause()
                assert selector.value == ""
                _assert_selector_painted(app, selector, "Default")
                await pilot.press("enter")
                await pilot.pause()
                assert selector.expanded
                _assert_selector_painted(app, selector, "Default")
                await pilot.press("escape")
                await pilot.pause()
                assert not selector.expanded
                assert app.screen is screen
                _assert_selector_painted(app, selector, "Default")
            assert app.session.executed == []

    asyncio.run(exercise())
