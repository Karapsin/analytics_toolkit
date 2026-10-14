from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from analytics_toolkit.sql_explorer import app as app_module
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from textual.document._document import Selection

from tests.sql._support.explorer import FakeSession

if TYPE_CHECKING:
    import pytest


def test_independent_columns_restore_after_short_lines_and_history() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "abcdefghij\nx\nabcdefghij\nabcdefghij"
            editor._set_selections(Selection.cursor((0, 8)), [Selection.cursor((2, 3))])
            editor.move_rows(1)
            before = editor.cursor_selections
            assert before == (Selection.cursor((1, 1)), Selection.cursor((3, 3)))
            editor._apply_batch_edit("!")
            editor.action_undo()
            assert editor.cursor_selections == before
            editor.move_rows(1)
            assert editor.cursor_selections == (Selection.cursor((2, 8)), Selection.cursor((3, 3)))
            editor.move_rows(-1)
            assert editor.cursor_selections == (Selection.cursor((1, 1)), Selection.cursor((2, 3)))

    asyncio.run(exercise())


def test_same_row_edit_paste_and_atomic_undo_redo() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "abcdef"
            editor._set_selections(Selection.cursor((0, 5)), [Selection.cursor((0, 1))])
            before = editor.cursor_selections
            editor._edit_cursors("", fragments=("X", "YZ"))
            assert editor.text == "aXbcdeYZf"
            assert editor.cursor_count == 2
            editor.action_undo()
            assert editor.text == "abcdef"
            assert editor.cursor_selections == before
            assert editor.selection == Selection.cursor((0, 5))
            editor.action_redo()
            assert editor.text == "aXbcdeYZf"
            assert editor.selection == Selection.cursor((0, 8))

    asyncio.run(exercise())


def test_line_boundary_movement_collapse_and_exact_position_toggle() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "abcd\nabcdef\nxyz"
            editor._set_selections(Selection.cursor((0, 3)), [Selection.cursor((1, 1))])
            editor.action_add_cursor_below()
            assert editor.cursor_count == 3
            assert editor.selection == Selection.cursor((1, 3))
            editor.action_add_cursor_above()
            assert editor.cursor_count == 2
            assert Selection.cursor((1, 1)) in editor.cursor_selections
            editor._set_selections(Selection.cursor((0, 4)), [Selection.cursor((1, 5))])
            editor.action_cursor_right()
            assert editor.cursor_selections == (Selection.cursor((1, 0)), Selection.cursor((1, 6)))
            editor.action_cursor_left()
            assert editor.cursor_selections == (Selection.cursor((0, 4)), Selection.cursor((1, 5)))
            editor._set_selections(Selection((1, 5), (1, 2)), [Selection((0, 1), (0, 3))])
            editor.action_cursor_left()
            assert editor.cursor_selections == (Selection.cursor((0, 1)), Selection.cursor((1, 2)))

    asyncio.run(exercise())


def test_overlapping_pair_deletions_remove_union_once() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "()tail"
            editor._set_selections(Selection.cursor((0, 1)), [Selection.cursor((0, 2))])
            editor.action_delete_left()
            assert editor.text == "tail"
            assert editor.cursor_count == 1
            editor.action_undo()
            assert editor.text == "()tail"
            assert editor.cursor_count == 2
            editor.action_redo()
            assert editor.text == "tail"

    asyncio.run(exercise())


def test_reversed_overlapping_selections_keep_primary_direction() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "abcdefgh"
            editor._set_selections(Selection((0, 6), (0, 3)), [Selection((0, 1), (0, 4))])
            assert editor.selection == Selection((0, 6), (0, 1))
            editor._apply_batch_edit("X")
            assert editor.text == "aXgh"
            editor.action_undo()
            assert editor.selection == Selection((0, 6), (0, 1))

    asyncio.run(exercise())


def test_multicursor_indent_unindent_is_one_transaction_and_preserves_direction() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "one\ntwo\nthree"
            editor._set_selections(Selection((2, 5), (2, 0)), [Selection((0, 0), (1, 3))])
            before = editor.cursor_selections
            editor.action_indent()
            assert editor.text == "    one\n    two\n    three"
            assert editor.selection.start > editor.selection.end
            editor.action_unindent()
            assert editor.text == "one\ntwo\nthree"
            assert editor.cursor_selections == before
            editor.action_undo()
            assert editor.text == "    one\n    two\n    three"
            editor.action_undo()
            assert editor.text == "one\ntwo\nthree"
            assert editor.cursor_selections == before

    asyncio.run(exercise())


def test_empty_cut_clears_fragments_without_deleting_or_changing_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clipboard = ["external"]
    monkeypatch.setattr(app_module.pyperclip, "copy", lambda text: clipboard.__setitem__(0, text))
    monkeypatch.setattr(app_module.pyperclip, "paste", lambda: clipboard[0])

    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "SELECT 1;\nSELECT 2;"
            editor._set_selections(Selection.cursor((1, 0)), [Selection.cursor((0, 0))])
            editor.action_cut()
            assert clipboard[0] == ""
            assert app.clipboard_fragments("") == ("", "")
            assert editor.execution_text() == "SELECT 1;\nSELECT 2;"
            assert not editor.paste_clipboard(app.paste_from_explorer_clipboard())
            editor.collapse_to_active()
            editor.copy_to_clipboard()
            assert clipboard[0] == editor.text
            assert app.clipboard_fragments(clipboard[0]) is None

    asyncio.run(exercise())


def test_empty_external_clipboard_invalidates_fragments_even_before_paste(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module.pyperclip, "copy", lambda _: None)
    monkeypatch.setattr(app_module.pyperclip, "paste", lambda: "")

    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        app.copy_to_explorer_clipboard("a\nb", fragments=("a", "b"))
        assert app.paste_from_explorer_clipboard() == ""
        assert app.clipboard_fragments("a\nb") is None

    asyncio.run(exercise())


def test_vertical_extension_keeps_each_anchor_and_intended_column() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            editor = app.active_workspace.editor
            editor.text = "abcdefghij\nx\nabcdefghij\nabcdefghij\ny\nabcdefghij"
            editor._set_selections(Selection.cursor((0, 8)), [Selection.cursor((3, 3))])
            await pilot.press("ctrl+shift+down", "ctrl+shift+down")
            assert editor.cursor_selections == (
                Selection((0, 8), (2, 8)),
                Selection((3, 3), (5, 3)),
            )
            await pilot.press("ctrl+shift+up", "ctrl+shift+up")
            assert editor.cursor_selections == (
                Selection.cursor((0, 8)),
                Selection.cursor((3, 3)),
            )

    asyncio.run(exercise())


def test_click_at_clamped_position_resets_preferred_column() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 40)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "abcdefghij\nx\nabcdefghij"
            editor._set_selections(Selection.cursor((0, 8)), [])
            editor.focus()
            await pilot.press("down")
            assert editor.cursor_location == (1, 1)
            assert editor.logical_column == 8
            viewport = editor.scrollable_content_region
            assert await pilot.click(
                offset=(viewport.x + editor.gutter_width + 1, viewport.y + 1)
            )
            assert editor.cursor_location == (1, 1)
            assert editor.logical_column == 1
            await pilot.press("down")
            assert editor.cursor_location == (2, 1)
            assert editor.cursor_count == 1
            assert editor.text == "abcdefghij\nx\nabcdefghij"

    asyncio.run(exercise())
