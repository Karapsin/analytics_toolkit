from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

from analytics_toolkit.sql_explorer import app as app_module
from analytics_toolkit.sql_explorer.app import SqlEditor, SqlExplorerApp
from textual.document._document import Selection
from textual.widgets import Input, TextArea

from tests.sql.explorer.app import FakeSession

if TYPE_CHECKING:
    import pytest


def test_move_commands_use_one_based_line_starts_and_clear_extra_cursors() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            command = application.query_one("#command-input", Input)
            editor.text = "zero\none\ntwo"
            editor.cursor_location = (2, 2)
            await pilot.press("shift+up")
            assert editor.cursor_count == 2

            command.value = ":mv 2"
            command.focus()
            await pilot.press("enter")
            assert editor.cursor_count == 1
            assert editor.selection == Selection.cursor((1, 0))
            assert application.focused is command

            editor.cursor_location = (1, 2)
            command.value = "mvs 1"
            command.focus()
            await pilot.press("enter")
            assert editor.selection == Selection((1, 2), (0, 0))
            assert application.focused is command

            command.value = "mv 0"
            command.focus()
            await pilot.press("enter")
            assert "between 1 and 3" in str(application.query_one("#result-message").render())
            assert application.focused is command

    asyncio.run(exercise())


def test_copy_and_paste_commands_apply_to_every_cursor(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    copied: list[str] = []
    monkeypatch.setattr(app_module.pyperclip, "copy", copied.append)
    monkeypatch.setattr(app_module.pyperclip, "paste", lambda: "X")

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            command = application.query_one("#command-input", Input)
            editor.text = "aa\nbb\ncc"
            editor.selection = Selection((0, 0), (0, 1))
            await pilot.press("shift+down")
            editor.selection = Selection((1, 0), (1, 1))
            command.value = "cp"
            command.focus()
            await pilot.press("enter")
            assert copied[-1] == "a\nb"

            command.value = "pst"
            await pilot.press("enter")
            assert editor.text == "Xa\nXb\ncc"
            assert editor.cursor_count == 2

    asyncio.run(exercise())


def test_escape_collapses_extra_cursors_before_changing_panes() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "one\ntwo"
            editor.cursor_location = (1, 1)
            await pilot.press("shift+up")
            assert editor.cursor_count == 2
            await pilot.press("escape")
            assert editor.cursor_count == 1
            assert application.focused is editor
            await pilot.press("escape")
            assert application.focused.id == "command-input"

    asyncio.run(exercise())


def test_cursor_commands_report_invalid_usage_and_empty_paste(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(app_module.pyperclip, "paste", lambda: "")

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            command = application.query_one("#command-input", Input)
            for value in ("mv", "mvs nope", "cp unexpected", "pst unexpected", "pst"):
                command.value = value
                command.focus()
                await pilot.press("enter")
            command.focus()
            application.action_copy_focused()
            assert "Clipboard is empty" in str(application.query_one("#notice").render())

    asyncio.run(exercise())


def test_cursor_edit_edge_actions_cover_single_and_multi_cursor_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            editor = application.query_one(SqlEditor)
            editor.text = "a\nb"
            editor.cursor_location = (0, 0)
            editor.action_add_cursor_above()
            assert editor.cursor_count == 1

            editor.cursor_location = (0, 1)
            editor.action_cursor_right()
            editor.action_delete_right()
            editor.action_cursor_left()
            editor.action_delete_left()
            assert editor.text == "\n"

            editor.text = "\nb"
            editor.cursor_location = (0, 0)
            editor.action_add_cursor_below()
            editor.action_completion_or_indent()
            assert editor.text == "    \n    b"
            editor.action_indent()
            assert editor.text == "        \n        b"

            await editor._on_paste(SimpleNamespace(text="x"))
            assert editor.text == "        x\n        xb"
            editor.read_only = True
            await editor._on_paste(SimpleNamespace(text="ignored"))
            editor.read_only = False
            editor._set_selections(Selection.cursor((0, 0)), [Selection.cursor((0, 0))])

            async def ignore_mouse_down(*_args: object) -> None:
                return None

            monkeypatch.setattr(TextArea, "_on_mouse_down", ignore_mouse_down)
            await editor._on_mouse_down(SimpleNamespace())

    asyncio.run(exercise())
