from __future__ import annotations

import asyncio

import pytest
from analytics_toolkit.sql_explorer import app as app_module
from analytics_toolkit.sql_explorer.app import SqlEditor, SqlExplorerApp
from analytics_toolkit.sql_explorer.clipboard import paste_fragments
from textual import events
from textual.document._document import Selection

from tests.sql.explorer.app import FakeSession


@pytest.mark.parametrize(
    ("value", "count", "copied", "expected"),
    [
        ("a\nb", 2, None, ("a", "b")),
        ("a\r\nb\r\n", 2, None, ("a", "b")),
        ("a\rb", 2, None, ("a", "b")),
        ("a\rb\r", 2, None, ("a", "b")),
        ("a\n\n", 2, None, ("a", "")),
        ("a\r\n\r\n", 2, None, ("a", "")),
        ("a\nb", 3, None, ("a\nb",) * 3),
        ("a\nb", 1, ("a", "b"), ("a\nb",)),
        ("a\nb\nc", 2, ("a\nb", "c"), ("a\nb", "c")),
        ("a\nb", 2, ("a", "", "b"), ("a", "b")),
        ("a\n", 2, ("a", ""), ("a", "")),
    ],
)
def test_paste_distribution(value, count, copied, expected) -> None:
    assert paste_fragments(value, count, copied) == expected


def test_vertical_selection_preserves_anchor_and_preferred_column() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "abcdef\nx\n\nabcdef"
            editor.cursor_location = (3, 4)
            await pilot.press("ctrl+shift+up", "ctrl+shift+up", "ctrl+shift+up")
            assert editor.selection == Selection((3, 4), (0, 4))
            assert editor.cursor_count == 1
            await pilot.press("ctrl+shift+up")
            assert editor.selection == Selection((3, 4), (0, 4))
            await pilot.press("ctrl+shift+down", "ctrl+shift+down", "ctrl+shift+down")
            assert editor.selection == Selection.cursor((3, 4))
            await pilot.press("ctrl+shift+down")
            assert editor.selection == Selection.cursor((3, 4))

            editor.cursor_location = (1, 1)
            await pilot.press("ctrl+shift+up", "ctrl+shift+down", "ctrl+shift+down")
            assert editor.selection == Selection((1, 1), (2, 0))
            editor._set_selections(Selection.cursor((3, 2)), [Selection.cursor((1, 2))])
            await pilot.press("ctrl+shift+up")
            assert editor.cursor_count == 2
            assert editor.cursor_selections == (
                Selection((1, 2), (0, 2)),
                Selection((3, 2), (2, 0)),
            )
            editor.text = "aa\nbb\ncc"
            editor._set_selections(Selection((0, 0), (1, 1)), [Selection((1, 0), (2, 1))])
            assert editor.cursor_selections == (Selection((0, 0), (2, 1)),)
            editor._apply_batch_edit("X")
            assert editor.text == "Xc"
            editor._set_selections(Selection((0, 0), (0, 1)), [Selection.cursor((0, 0))])
            assert editor.cursor_count == 1

    asyncio.run(exercise())


def test_multicursor_clipboard_keyboard_commands_and_undo(monkeypatch) -> None:
    clipboard = [""]
    monkeypatch.setattr(app_module.pyperclip, "copy", lambda value: clipboard.__setitem__(0, value))
    monkeypatch.setattr(app_module.pyperclip, "paste", lambda: clipboard[0])

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "alpha\nbeta\ngamma"
            editor._set_selections(Selection((0, 0), (0, 2)), [Selection((1, 0), (1, 3))])
            await pilot.press("ctrl+c")
            assert clipboard[0] == "al\nbet"
            editor._set_selections(Selection.cursor((2, 5)), [Selection.cursor((0, 5))])
            await pilot.press("ctrl+v")
            assert editor.text == "alphaal\nbeta\ngammabet"
            await pilot.press("ctrl+z")
            assert editor.text == "alpha\nbeta\ngamma"
            assert editor.cursor_selections == (Selection.cursor((0, 5)), Selection.cursor((2, 5)))
            editor._apply_batch_edit("")
            await pilot.press("ctrl+y")
            assert editor.text == "alphaal\nbeta\ngammabet"
            assert editor.cursor_selections == (Selection.cursor((0, 7)), Selection.cursor((2, 8)))

            editor.text = "one\ntwo"
            editor._set_selections(Selection.cursor((1, 0)), [Selection((0, 0), (0, 1))])
            application._command_copy([])
            assert clipboard[0] == "o\n"
            application._command_paste([])
            assert editor.text == "one\ntwo"
            editor._set_selections(Selection.cursor((1, 0)), [Selection.cursor((0, 0))])
            await pilot.press("ctrl+c")
            assert clipboard[0] == ""
            application._command_copy([])
            assert clipboard[0] == ""
            await pilot.press("ctrl+v")
            assert editor.text == "one\ntwo"

            clipboard[0] = "X\nY"
            await pilot.press("ctrl+v")
            assert editor.text == "Xone\nYtwo"
            assert application.clipboard_fragments(clipboard[0]) is None
            application.copy_to_explorer_clipboard("Z")
            await editor._on_paste(events.Paste("Z"))
            assert editor.text == "XZone\nYZtwo"

    asyncio.run(exercise())


def test_multiline_fragments_cut_and_clipboard_replacement(monkeypatch) -> None:
    clipboard = [""]
    monkeypatch.setattr(app_module.pyperclip, "copy", lambda value: clipboard.__setitem__(0, value))
    monkeypatch.setattr(app_module.pyperclip, "paste", lambda: clipboard[0])

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "a\nb\nc"
            editor._set_selections(Selection((0, 0), (1, 1)), [Selection((2, 0), (2, 1))])
            await pilot.press("ctrl+x")
            assert clipboard[0] == "a\nb\nc"
            assert application.clipboard_fragments(clipboard[0]) == ("a\nb", "c")
            editor.text = "X\nY"
            editor._set_selections(Selection.cursor((1, 1)), [Selection.cursor((0, 1))])
            await pilot.press("ctrl+v")
            assert editor.text == "Xa\nb\nYc"
            application.copy_to_explorer_clipboard("replacement")
            assert application.clipboard_fragments("replacement") is None

    asyncio.run(exercise())


def test_carets_draw_bars_before_occupied_and_blank_cells() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "select 1\n\nlast"
            editor._set_selections(
                Selection.cursor((0, 2)), [Selection.cursor((1, 0)), Selection.cursor((2, 4))]
            )
            for focused in (True, False):
                if focused:
                    editor.focus()
                else:
                    application.query_one("#command-input").focus()
                # Compare with the same cells before secondary carets are drawn.
                # Textual may resolve their inherited background on newer versions.
                editor._set_selections(Selection.cursor((0, 2)), [])
                await pilot.pause()
                backgrounds = {
                    row: next(
                        iter(
                            editor.render_line(row).crop(
                                editor.gutter_width + column, editor.gutter_width + column + 1
                            )
                        )
                    ).style.bgcolor
                    for row, column in ((1, 0), (2, 4))
                }
                editor._set_selections(
                    Selection.cursor((0, 2)),
                    [Selection.cursor((1, 0)), Selection.cursor((2, 4))],
                )
                await pilot.pause()
                for row in range(3):
                    strip = editor.render_line(row)
                    bars = [segment for segment in strip if segment.text == "▌"]
                    assert len(bars) == 1
                    if row == 0:
                        assert "se▌lect 1" in strip.text
                        following = strip.crop(editor.gutter_width + 3, editor.gutter_width + 4)
                        assert following.text == "l"
                        assert all(not segment.style.underline for segment in following)
                    else:
                        assert len(bars) == 1
                        assert bars[0].style.bold is False
                        assert bars[0].style.bgcolor == backgrounds[row]
                        assert not bars[0].style.reverse
                        assert not bars[0].style.underline
            assert editor.text == "select 1\n\nlast"

    asyncio.run(exercise())


def test_undo_restores_cursors_before_continued_typing_and_replacement() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            editor = application.query_one(SqlEditor)
            editor.action_undo()
            editor.action_redo()
            editor.text = "aa\nbb"
            editor._set_selections(Selection.cursor((1, 2)), [Selection.cursor((0, 2))])
            application.copy_to_explorer_clipboard("XYZ")
            editor.paste_clipboard("XYZ")
            editor.action_undo()
            editor._apply_batch_edit("!")
            assert editor.text == "aa!\nbb!"
            editor.action_redo()
            assert editor.text == "aa!\nbb!"
            editor.action_undo()
            assert editor.cursor_selections == (Selection.cursor((0, 2)), Selection.cursor((1, 2)))

            editor._set_selections(Selection((1, 0), (1, 2)), [Selection((0, 0), (0, 2))])
            editor._apply_batch_edit("X")
            editor.action_undo()
            assert editor.cursor_selections == (
                Selection((0, 0), (0, 2)),
                Selection((1, 0), (1, 2)),
            )
            editor.action_redo()
            assert editor.text == "X\nX"
            editor.replace("Y", (0, 0), (0, 1))
            editor.action_undo()
            assert editor.text == "X\nX"
            assert editor.cursor_count == 1
            editor.action_redo()
            assert editor.text == "Y\nX"

            editor.text = ""
            editor.action_delete_left()
            editor._apply_batch_edit("X")
            editor._apply_batch_edit("")
            editor.action_undo()
            assert editor.text == ""
            assert editor.selection == Selection.cursor((0, 0))

            editor.text = "aa\nbb\ncc"
            editor._set_selections(Selection((2, 1), (1, 0)), [Selection((0, 0), (1, 1))])
            assert editor.selection == Selection((2, 1), (0, 0))
            assert editor.cursor_count == 1

    asyncio.run(exercise())
