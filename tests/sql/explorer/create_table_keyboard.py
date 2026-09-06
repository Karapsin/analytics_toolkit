from __future__ import annotations

import asyncio
from typing import Any

from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.create_table_screen import ColumnRow, CreateTableScreen
from textual.widgets import Button, Checkbox, Collapsible, Input, OptionList, Select, TextArea

from tests.sql.explorer.app import FakeSession


async def seek(pilot: Any, app: Any, predicate: Any) -> None:
    for _ in range(60):
        if predicate(app.focused):
            return
        if app.screen.type_matches:
            await pilot.press("escape")
        await pilot.press("down")
    msg = "Control was not reachable using arrows"
    raise AssertionError(msg)


def test_schema_form_is_operable_with_arrows_and_enter_only() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(110, 45)) as pilot:
            app._command_create_table([])
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CreateTableScreen)
            await pilot.press(*"sandbox.people", "down")
            assert app.focused.id == "create-table-source"
            await pilot.press("down", *"id", "down", "b", "i", "enter")
            assert screen.query_one(".create-column-type", Input).value == "BIGINT"
            await pilot.press("down", "down", "enter")
            assert len(screen.query(ColumnRow)) == 2
            await pilot.press(*"name", "down", "t", "e", "enter", "down", "enter")
            assert len(screen.query(ColumnRow)) == 1
            await seek(
                pilot, app, lambda w: isinstance(w, Checkbox) and w.id == "create-skip_if_exists"
            )
            await pilot.press("enter")
            assert screen.query_one("#create-skip_if_exists", Checkbox).value
            await pilot.press("down", "right")
            assert screen.query_one("#create-drop_if_exists", Checkbox).value
            assert not screen.query_one("#create-skip_if_exists", Checkbox).value
            await pilot.press("left")
            assert not screen.query_one("#create-drop_if_exists", Checkbox).value
            await pilot.press("down", "enter")
            assert not screen.query_one(Collapsible).collapsed
            await seek(pilot, app, lambda w: isinstance(w, Input) and w.name == "order_by")
            await pilot.press("i", "d", "left", "right", "up", "down")
            assert app.focused.name == "order_by"
            await seek(
                pilot, app, lambda w: isinstance(w, Button) and w.id == "create-table-submit"
            )
            await pilot.press("enter")
            await pilot.pause()
            assert app.session.executed[0].options["order_by"] == "id"
            assert app.session.executed[0].options["table_schema"] == {"id": "BIGINT"}

    asyncio.run(exercise())


def test_sql_source_navigation_dropdown_and_multiline_editing() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(110, 45)) as pilot:
            app._command_create_table([])
            await pilot.pause()
            screen = app.screen
            await pilot.press(*"sandbox.copy", "enter", "enter", "down", "enter")
            assert screen.query_one("#create-table-source", Select).value == "from_sql"
            await pilot.press("down", *"select 1", "enter", *"from source", "up", "down")
            assert screen.query_one(TextArea).text == "select 1\nfrom source"
            await pilot.press("left", "right", "down", "enter")
            assert screen.query_one("#create-insert_data", Checkbox).value
            await pilot.press("up", "up")
            assert isinstance(app.focused, TextArea)
            await pilot.press("up")
            assert app.focused.id == "create-table-source"
            await pilot.press("enter", "escape")
            assert app.screen is screen
            assert not screen.query_one("#create-table-source", Select).expanded
            await seek(
                pilot, app, lambda w: isinstance(w, Button) and w.id == "create-table-cancel"
            )
            await pilot.press("enter")
            assert app.session.executed == []

    asyncio.run(exercise())


def test_tab_keys_never_navigate_command_or_form() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            for surface in ("command", "form"):
                if surface == "command":
                    await pilot.press("escape")
                elif surface == "form":
                    app._command_create_table([])
                    await pilot.pause()
                focused = app.focused
                text = app.active_workspace.editor.text
                await pilot.press("tab", "shift+tab", "ctrl+tab", "ctrl+shift+tab", "alt+tab")
                assert app.focused is focused
                assert app.active_workspace.editor.text == text
                assert len(app._workspaces) == 1

    asyncio.run(exercise())


def test_creation_sql_indentation_and_advanced_choices() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(110, 45)) as pilot:
            app.session.database.backend = "ch"
            app._command_create_table([])
            await pilot.pause()
            screen = app.screen
            assert not any(w.name == "retry_cnt" for w in screen.query(Input))
            assert not any(w.name == "timeout_increment" for w in screen.query(Input))
            source = screen.query_one("#create-table-source", Select)
            source.focus()
            await pilot.press("right")
            assert source.value == "from_sql"
            sql_field = screen.query_one(TextArea)
            sql_field.focus()
            await pilot.press("tab", *"select 1", "enter", *"select 2")
            assert sql_field.text == "    select 1\nselect 2"
            await pilot.press("up", "shift+tab")
            assert sql_field.text == "select 1\nselect 2"
            await pilot.press("down", "left", "right")
            assert sql_field.cursor_location[0] == 1
            screen.query_one(Collapsible).collapsed = False
            choice = next(w for w in screen.query(Select) if w.name == "ch_distributed_table")
            choice.focus()
            await pilot.press("right")
            assert choice.value == "True"
            await pilot.press("left")
            assert choice.value == ""
            await pilot.press("enter", "down", "up", "enter")
            app.action_complete()
            screen.query_one("#create-table-cancel", Button).focus()
            await pilot.press("right", "left")
            screen.query_one("#create-table-cancel", Button).press()
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert app.session.executed == []
            app._command_create_table(["extra"])
            assert len(app.screen_stack) == 1
            app.active_workspace.busy = True
            app._command_create_table([])
            assert len(app.screen_stack) == 1

    asyncio.run(exercise())


def test_creation_type_mouse_selection_cancel_and_inline_validation() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(110, 45)) as pilot:
            app._command_create_table([])
            await pilot.pause()
            screen = app.screen
            field = screen.query_one(".create-column-type", Input)
            field.focus()
            field.value = "BI"
            await pilot.pause()
            menu = screen.query_one("#create-type-options")
            await pilot.pause(0.1)
            assert menu.option_count == 1, (menu.option_count, screen.type_matches)
            assert menu.display
            assert menu.region.width
            assert await pilot.click("#create-type-options", offset=(2, 0)), (
                menu.region,
                menu.content_region,
            )
            await pilot.pause()
            assert field.value == "BIGINT"
            field.value = "T"
            await pilot.pause()
            await pilot.press("down", "up")
            assert screen.type_index == 0
            screen.action_cancel()
            assert not screen.type_matches
            assert len(app.screen_stack) == 2
            await pilot.click("#create-table-submit")
            assert "table name" in str(screen.query_one("#create-table-notice").render())
            screen.query_one("#create-table-cancel", Button).press()
            await pilot.pause()
            assert len(app.screen_stack) == 1

    asyncio.run(exercise())


def test_creation_ignores_unrelated_control_events() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            app._command_create_table([])
            await pilot.pause()
            screen = app.screen
            unrelated = OptionList("option", id="unrelated")
            screen.on_option_list_option_selected(OptionList.OptionSelected(unrelated, 0))
            await screen.on_button_pressed(Button.Pressed(Button("unrelated")))
            await screen.on_button_pressed(
                Button.Pressed(Button("detached", classes="remove-column"))
            )
            assert len(app.screen_stack) == 2
            assert len(screen.query(ColumnRow)) == 1
            assert app.session.executed == []

    asyncio.run(exercise())
