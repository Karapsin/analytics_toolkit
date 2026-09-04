from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from analytics_toolkit.sql_explorer import app as app_module
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.file_commands import NewSqlFileScreen
from analytics_toolkit.sql_explorer.inputs import EditableInput
from analytics_toolkit.sql_explorer.widgets import FileNavigationScreen
from textual.document._document import Selection
from textual.events import Paste
from textual.widgets import Input, TextArea

from tests.sql.explorer.app import FakeSession

if TYPE_CHECKING:
    import pytest


def test_editable_input_selection_clipboard_history_and_word_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clipboard: list[str] = []
    monkeypatch.setattr(app_module.pyperclip, "copy", clipboard.append)
    monkeypatch.setattr(app_module.pyperclip, "paste", lambda: clipboard[-1])

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            await pilot.press("ctrl+f")
            field = application.query_one("#find-pattern", EditableInput)
            field.value = "alpha beta"
            field.cursor_position = len(field.value)

            await pilot.press("ctrl+shift+left")
            assert field.selected_text == "beta"
            assert field._value.spans
            await pilot.press("backspace")
            assert field.value == "alpha "
            await pilot.press("ctrl+z")
            assert field.value == "alpha beta"
            await pilot.press("ctrl+y")
            assert field.value == "alpha "

            field.value = "copy me"
            field.cursor_position = len(field.value)
            await pilot.press("meta+a", "meta+c", "ctrl+x")
            assert clipboard[-1] == "copy me"
            assert field.value == ""
            await pilot.press("ctrl+v")
            assert field.value == "copy me"
            await pilot.press("ctrl+a", "x", "ctrl+z", "ctrl+shift+z")
            assert field.value == "x"

    asyncio.run(exercise())


def test_every_explorer_single_line_field_uses_editable_input(tmp_path) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            application.action_open_find()
            for selector in ("#command-input", "#find-pattern", "#replace-pattern"):
                assert isinstance(application.query_one(selector, Input), EditableInput)
            application.active_workspace.find_bar.action_close()

            navigation = FileNavigationScreen(tmp_path)
            await application.push_screen(navigation)
            assert isinstance(navigation.query_one("#navigation-path", Input), EditableInput)
            navigation.dismiss(None)
            await pilot.pause()

            filename = NewSqlFileScreen()
            await application.push_screen(filename)
            assert isinstance(filename.query_one("#new-file-name", Input), EditableInput)
            filename.dismiss(None)

    asyncio.run(exercise())


def test_editable_input_selection_editing_and_empty_history_branches(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clipboard_value = {"value": ""}
    monkeypatch.setattr(
        app_module.pyperclip,
        "copy",
        lambda value: clipboard_value.__setitem__("value", value),
    )
    monkeypatch.setattr(
        app_module.pyperclip,
        "paste",
        lambda: clipboard_value["value"],
    )

    async def exercise() -> None:  # noqa: PLR0915
        application = SqlExplorerApp(FakeSession())
        application._update_editor_status()
        application.on_text_area_selection_changed(
            TextArea.SelectionChanged(Selection.cursor((0, 0)), TextArea())
        )
        async with application.run_test() as pilot:
            application.query_one("#new-tab").focus()
            await pilot.pause()
            application.action_copy_focused()
            field = application.query_one("#command-input", EditableInput)
            field.focus()
            field.value = "alpha beta"
            field.cursor_position = 5

            field.action_cursor_right_word(True)
            assert field.selected_text == " "
            field.action_cursor_left()
            assert field.cursor_position == 5
            field.action_cursor_right(True)
            field.action_cursor_right()
            assert field.cursor_position == 6

            field.action_home(True)
            assert field.selected_text == "alpha "
            field.action_home()
            assert field.cursor_position == 0
            field.action_end(True)
            assert field.selected_text == "alpha beta"
            field.action_end()
            assert field.cursor_position == len(field.value)

            field.action_select_all()
            field.value = "x"
            assert field.selection == (0, 1)
            field.cursor_position = 0
            field._selection_anchor = None
            field.action_copy()
            field.action_cut()
            assert field.value == "x"
            field.action_paste()
            assert field.value == "x"

            field.value = "alpha beta"
            field.cursor_position = len(field.value)
            field.action_delete_left()
            assert field.value == "alpha bet"
            field.value = "alpha beta"
            field.cursor_position = 0
            field.action_delete_right()
            assert field.value == "lpha beta"
            field.value = "alpha beta"
            field.cursor_position = len(field.value)
            field.action_delete_left_word()
            assert field.value == "alpha "
            field.value = "alpha beta"
            field.cursor_position = 0
            field.action_delete_right_word()
            assert field.value == "beta"
            field.value = "alpha beta"
            field.cursor_position = len(field.value)
            field.action_delete_left_all()
            assert field.value == ""
            field.value = "alpha beta"
            field.cursor_position = 0
            field.action_delete_right_all()
            assert field.value == ""

            field._undo_stack.clear()
            field._redo_stack.clear()
            field.action_undo()
            field.action_redo()
            field._on_paste(Paste("first\nsecond"))
            assert field.value == "first"
            field._on_paste(Paste(""))

            field.value = "abcd"
            await pilot.click("#command-input", offset=(3, 0))
            await pilot.click("#command-input", offset=(1, 0), shift=True)
            assert field.selected_text

            restricted = EditableInput(value="a", restrict=r"[a-z]*", id="restricted")
            await application.active_workspace.query_one("#command-row").mount(restricted)
            restricted.action_select_all()
            restricted.insert_text_at_cursor("1")
            assert restricted.value == "a"

            field.value = "abc"
            field.cursor_position = 2
            field._selection_anchor = None
            field.action_cursor_left(True)
            field.action_cursor_left(True)
            assert field.selected_text == "ab"
            field.value = ""
            field.cursor_position = 0
            field._selection_anchor = None
            field.action_delete_left()
            assert field.value == ""

    asyncio.run(exercise())
