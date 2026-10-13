from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from analytics_toolkit.sql_explorer import app as app_module
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from textual.document._document import Selection

from tests.sql._support.explorer import FakeSession

if TYPE_CHECKING:
    import pytest


def test_independent_columns_boundaries_and_exact_position_toggle() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            editor = application.active_workspace.editor
            editor.text = "abcdef\nx\nabcdef\nabcdef"
            editor._set_selections(Selection.cursor((0, 5)), [Selection.cursor((2, 2))])
            editor.action_cursor_down()
            assert editor.cursor_selections == (
                Selection.cursor((1, 1)),
                Selection.cursor((3, 2)),
            )
            editor.action_cursor_down()
            assert editor.cursor_selections == (
                Selection.cursor((2, 5)),
                Selection.cursor((3, 2)),
            )
            editor.action_cursor_up()
            assert editor.cursor_selections == (
                Selection.cursor((1, 1)),
                Selection.cursor((2, 2)),
            )
            editor.action_add_cursor_below()
            assert editor.selection == Selection.cursor((2, 5))
            assert editor.cursor_count == 3
            editor.action_add_cursor_above()
            assert editor.cursor_count == 2
            assert Selection.cursor((2, 2)) in editor.cursor_selections

            editor._set_selections(Selection.cursor((1, 0)), [Selection.cursor((0, 0))])
            editor.action_cursor_left()
            assert editor.cursor_selections == (
                Selection.cursor((0, 0)),
                Selection.cursor((0, 6)),
            )
            editor.action_cursor_right()
            assert editor.cursor_selections == (
                Selection.cursor((0, 1)),
                Selection.cursor((1, 0)),
            )
            editor._set_selections(Selection((0, 5), (0, 2)), [Selection((2, 1), (2, 4))])
            editor.action_cursor_left()
            assert editor.cursor_selections == (
                Selection.cursor((0, 2)),
                Selection.cursor((2, 1)),
            )
            editor._set_selections(Selection((0, 5), (0, 2)), [Selection((2, 1), (2, 4))])
            editor.action_cursor_right()
            assert editor.cursor_selections == (
                Selection.cursor((0, 5)),
                Selection.cursor((2, 4)),
            )

    asyncio.run(exercise())


def test_same_row_editing_and_overlapping_paired_deletion_are_atomic() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            editor = application.active_workspace.editor
            editor.text = "abcd"
            editor._set_selections(Selection.cursor((0, 3)), [Selection.cursor((0, 1))])
            before = editor.cursor_selections
            editor._apply_batch_edit("X")
            assert editor.text == "aXbcXd"
            editor.action_undo()
            assert editor.text == "abcd"
            assert editor.cursor_selections == before
            editor.action_redo()
            assert editor.text == "aXbcXd"

            editor.text = "()x"
            editor._set_selections(Selection.cursor((0, 2)), [Selection.cursor((0, 1))])
            editor.action_delete_left()
            assert editor.text == "x"
            assert editor.cursor_count == 1
            editor.action_undo()
            assert editor.text == "()x"
            assert editor.cursor_count == 2

            editor.text = "ab\ncd"
            editor._set_selections(Selection.cursor((0, 2)), [Selection.cursor((1, 0))])
            editor.action_delete_right()
            assert editor.text == "abd"
            editor.action_undo()
            assert editor.text == "ab\ncd"
            editor._edit_cursors("(", paired=True)
            assert editor.text == "ab()\n(cd"
            editor.action_undo()
            editor._apply_batch_edit("\n")
            assert editor.text == "ab\n\n\ncd"

    asyncio.run(exercise())


def test_indent_once_preserves_reversed_selections_and_history_columns() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            editor = application.active_workspace.editor
            editor.text = "abcdef\nx\nabcdef"
            editor._set_selections(Selection((0, 4), (0, 2)), [Selection((0, 5), (0, 6))])
            before = editor.cursor_selections
            editor.action_indent()
            assert editor.text == "    abcdef\nx\nabcdef"
            assert editor.selection == Selection((0, 8), (0, 6))
            assert editor.cursor_count == 2
            editor.action_unindent()
            assert editor.text == "abcdef\nx\nabcdef"
            assert editor.cursor_selections == before
            editor.action_undo()
            assert editor.text.startswith("    abcdef")
            assert editor.selection == Selection((0, 8), (0, 6))

            editor.text = "abcdef\nx\nabcdef"
            editor._set_selections(Selection.cursor((0, 5)), [Selection.cursor((2, 3))])
            editor.action_cursor_down()
            assert editor.logical_column == 5
            editor._apply_batch_edit("!")
            editor.action_undo()
            assert editor.logical_column == 5
            editor._apply_batch_edit("")
            editor.action_redo()
            assert editor.text == "abcdef\nx!\nabc!def"
            editor.action_undo()
            editor.action_cursor_down()
            assert editor.cursor_selections == (
                Selection.cursor((2, 3)),
                Selection.cursor((2, 5)),
            )
            editor.text = "new"
            assert editor._cursor_edits == []
            assert editor.cursor_count == 1
            editor.action_redo()
            assert editor.text == "new"

    asyncio.run(exercise())


def test_empty_cut_external_empty_clipboard_and_unavailable_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clipboard = ["old"]
    monkeypatch.setattr(app_module.pyperclip, "copy", lambda value: clipboard.__setitem__(0, value))
    monkeypatch.setattr(app_module.pyperclip, "paste", lambda: clipboard[0])

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            editor = application.active_workspace.editor
            editor.text = "abc\ndef"
            editor._set_selections(Selection.cursor((0, 1)), [Selection.cursor((1, 2))])
            before = editor.cursor_selections
            editor.action_cut()
            assert clipboard[0] == ""
            assert application.clipboard_fragments("") == ("", "")
            assert editor.text == "abc\ndef"
            assert editor.cursor_selections == before

            editor._set_selections(Selection.cursor((1, 2)), [Selection((0, 0), (0, 1))])
            editor.action_cut()
            assert clipboard[0] == "a\n"
            assert application.clipboard_fragments(clipboard[0]) == ("a", "")
            assert editor.text == "bc\ndef"
            editor.action_undo()
            clipboard[0] = ""
            before = editor.cursor_selections
            editor.action_paste()
            assert editor.text == "abc\ndef"
            assert editor.cursor_selections == before
            assert application.clipboard_fragments("") is None

            def unavailable() -> str:
                message = "unavailable"
                raise app_module.pyperclip.PyperclipException(message)

            monkeypatch.setattr(app_module.pyperclip, "paste", unavailable)
            assert application.paste_from_explorer_clipboard() == "a\n"
            editor._set_selections(Selection.cursor((0, 0)), [])
            editor.copy_to_clipboard()
            assert clipboard[0] == editor.text
            editor.action_cut()
            assert clipboard[0] == editor.text
            assert editor.text == "abc\ndef"

    asyncio.run(exercise())


def test_caret_preserves_search_background_and_terminal_cell_positions() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(120, 40)) as pilot:
            editor = application.active_workspace.editor
            editor.text = "order_id orders order_date\n\t界x"
            editor.set_search_pattern("order")
            editor._set_selections(Selection((0, 5), (0, 0)), [])
            await pilot.pause()
            strip = editor.render_line(0)
            for column in (0, 1, 9, 16):
                segment = next(
                    iter(strip.crop(editor.gutter_width + column, editor.gutter_width + column + 1))
                )
                assert segment.style.bgcolor.name == "bright_yellow"
            caret = next(iter(strip.crop(editor.gutter_width, editor.gutter_width + 1)))
            assert caret.text == "│"
            assert caret.style.bold is False
            editor.clear_search()
            await pilot.pause()
            strip = editor.render_line(0)
            caret = next(iter(strip.crop(editor.gutter_width, editor.gutter_width + 1)))
            selected = next(iter(strip.crop(editor.gutter_width + 1, editor.gutter_width + 2)))
            assert caret.style.bgcolor == selected.style.bgcolor
            assert editor.text.startswith("order_id")
            editor._set_selections(
                Selection.cursor((1, 2)),
                [Selection.cursor((1, 1)), Selection.cursor((1, 3))],
            )
            application.active_workspace.command_input.focus()
            await pilot.pause()
            strip = editor.render_line(1)
            for column in (4, 6, 7):
                cell = strip.crop(editor.gutter_width + column, editor.gutter_width + column + 1)
                assert "".join(segment.text for segment in cell) == "│"
                assert all(segment.style.bold is False for segment in cell)
                assert all(segment.style.reverse is False for segment in cell)
            assert editor.text.endswith("\t界x")

    asyncio.run(exercise())
