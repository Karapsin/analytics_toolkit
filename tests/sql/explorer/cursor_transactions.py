from __future__ import annotations

import asyncio

from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from textual.document._document import Selection

from tests.sql._support.explorer import FakeSession


def test_empty_edit_preserves_independent_columns_and_redo() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            editor = application.active_workspace.editor
            editor.text = "abcdef\nx\nabcdef\nabcdef"
            editor._set_selections(Selection.cursor((0, 5)), [Selection.cursor((2, 2))])
            editor.action_cursor_down()
            before = editor._cursor_state()
            editor._apply_batch_edit("!")
            changed = editor.text
            after = editor._cursor_state()
            editor.action_undo()
            assert editor._cursor_state() == before

            editor._apply_batch_edit("")
            assert editor._cursor_state() == before
            editor.action_cursor_down()
            assert editor.cursor_selections == (
                Selection.cursor((2, 5)),
                Selection.cursor((3, 2)),
            )
            editor.action_redo()
            assert editor.text == changed
            assert editor._cursor_state() == after
            editor.action_undo()
            assert editor._cursor_state() == before

            editor._apply_batch_edit("?")
            editor.action_redo()
            assert editor.text == "abcdef\nx?\nabcdef\nab?cdef"

    asyncio.run(exercise())


def test_shift_tab_unindents_shared_rows_once_and_restores_all_anchors() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.active_workspace.editor
            editor.text = "    abcdefghi\n\tuvwxyz"
            editor._set_selections(
                Selection((0, 9), (0, 5)),
                [Selection((0, 10), (0, 12)), Selection((1, 2), (1, 5))],
            )
            before = editor._cursor_state()
            await pilot.press("shift+tab")
            assert editor.text == "abcdefghi\nuvwxyz"
            assert editor.selection == Selection((0, 5), (0, 1))
            assert editor.cursor_selections == (
                Selection((0, 5), (0, 1)),
                Selection((0, 6), (0, 8)),
                Selection((1, 1), (1, 4)),
            )
            after = editor._cursor_state()
            editor.action_undo()
            assert editor.text == "    abcdefghi\n\tuvwxyz"
            assert editor._cursor_state() == before
            editor.action_redo()
            assert editor.text == "abcdefghi\nuvwxyz"
            assert editor._cursor_state() == after

    asyncio.run(exercise())


def test_scrolled_carets_keep_every_search_match_highlighted() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(120, 40)) as pilot:
            editor = application.active_workspace.editor
            original = "x" * 200 + "order_id orders order_date"
            editor.text = original
            editor.set_search_pattern("order")
            editor._set_selections(
                Selection((0, 205), (0, 200)),
                [
                    Selection.cursor((0, 0)),
                    Selection.cursor((0, 209)),
                    Selection.cursor((0, 216)),
                ],
            )
            for focused in (True, False):
                if not focused:
                    application.active_workspace.command_input.focus()
                await pilot.pause()
                assert editor.scroll_x > 0
                strip = editor.render_line(0)
                for start in (200, 209, 216):
                    for column in range(start, start + 5):
                        x = editor.gutter_width + column - int(editor.scroll_x)
                        cell = strip.crop(x, x + 1)
                        segment = next(iter(cell))
                        assert segment.style.bgcolor.name == "bright_yellow"
                        if column == start:
                            assert segment.text == "│"
                            assert segment.style.bold is False
                            assert segment.style.reverse is False
                assert sum(segment.text == "│" for segment in strip) == 3
                assert editor.search_match_count == 3
                assert editor.text == original

    asyncio.run(exercise())
