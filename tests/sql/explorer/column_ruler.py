from __future__ import annotations

import asyncio

from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.movement import move_command
from analytics_toolkit.sql_explorer.ruler import ColumnRuler, column_numbers, ruler_labels
from textual.document._document import Selection

from tests.sql.explorer.app import FakeSession


def test_column_numbers_align_tabs_wide_and_combining_characters() -> None:
    assert column_numbers("", 0, 0, 4) == {}
    assert column_numbers("", 0, 80, 4) == {0: 1}
    assert column_numbers("abc", 0, 5, 4) == {0: 1, 1: 2, 2: 3, 3: 4}
    assert column_numbers("\t界a", 0, 8, 4) == {0: 1, 4: 2, 6: 3, 7: 4}
    assert column_numbers("\t界a", 3, 5, 4) == {1: 2, 3: 3, 4: 4}
    assert column_numbers("a\u0301b", 0, 3, 4) == {0: 1, 1: 3, 2: 4}


def test_ruler_labels_are_sparse_and_active_label_wins() -> None:
    numbers = column_numbers("x" * 30, 0, 31, 4)
    assert ruler_labels(numbers, 31, 1, "yellow").plain.strip() == "1       10        20        30"
    selected = ruler_labels(numbers, 31, 11, "yellow")
    assert "10" not in selected.plain
    assert selected.plain[9:11] == "11"
    assert len(selected.spans) == 1
    assert ruler_labels({}, 0, 1, "yellow").plain == ""
    assert ruler_labels({0: 100, 10: 110}, 11, 200, "yellow").plain == "        110"
    assert ruler_labels({0: 100}, 5, 100, "yellow").plain == "100  "


def test_all_rows_share_column_range_without_padding_text() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            editor = app.active_workspace.editor
            ruler = app.active_workspace.query_one(ColumnRuler)
            editor.text = "x" * 30 + "\ny\n"
            original = editor.text
            for row in range(3):
                editor.cursor_location = (row, 0)
                await pilot.pause()
                assert "30" in ruler.render().plain
                assert "40" not in ruler.render().plain
                assert editor.text == original
            move_command(editor, "mv", ["1", "100"])
            assert editor.document[0] == "x" * 30 + " " * 10
            assert editor.document.lines[1:] == ["y", ""]
            editor.cursor_location = (1, 0)
            await pilot.pause()
            assert "40" in ruler.render().plain
            assert editor.document.lines[1:] == ["y", ""]

    asyncio.run(exercise())


def test_ruler_grows_with_typing_and_bounded_absolute_movement() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            editor = app.active_workspace.editor
            ruler = app.active_workspace.query_one(ColumnRuler)
            assert ruler.render().plain.strip() == "1"
            editor.focus()
            await pilot.press("a", "b", "c")
            assert ruler.render().plain.strip() == "1  4"
            move_command(editor, "mv", ["1", "100"])
            await pilot.pause()
            assert editor.text == "abc" + " " * 10
            assert editor.cursor_location == (0, 13)
            assert "14" in ruler.render().plain
            assert "20" not in ruler.render().plain
            editor.action_undo()
            assert editor.text == "abc"
            move_command(editor, "mv", ["3", "5"])
            assert editor.text == "abc\n\n    "
            assert editor.cursor_location == (2, 4)
            editor.action_undo()
            assert editor.text == "abc"
            move_command(editor, "mv", ["E", "20"])
            assert editor.cursor_location == (0, 13)
            assert editor.document.line_count == 1

    asyncio.run(exercise())


def test_ruler_tracks_main_cursor_scroll_and_command_focus() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 40)) as pilot:
            workspace = app.active_workspace
            editor = workspace.editor
            ruler = workspace.query_one(ColumnRuler)
            editor.text = "x" * 300 + "\n" + "y" * 300
            editor.cursor_location = (0, 251)
            editor.add_cursors(1, 1)
            workspace.command_input.focus()
            await pilot.pause()
            assert editor.scroll_x > 0
            assert ruler.region.bottom == editor.region.y
            rendered = ruler.render()
            assert len(rendered.plain.splitlines()) == 1
            assert len(rendered.spans) == 1
            assert all(
                app.get_css_variables()["accent"] in str(span.style) for span in rendered.spans
            )
            active_x = editor.gutter_width + editor.gutter.left + 251 - int(editor.scroll_x)
            assert rendered.plain.splitlines()[-1][active_x] == "2"
            caret = editor.render_line(0).crop(
                active_x - editor.gutter.left, active_x - editor.gutter.left + 1
            )
            assert all(
                segment.style.bgcolor.triplet.hex == app.get_css_variables()["accent"].lower()
                for segment in caret
            )
            column = editor.cursor_location[1]
            line = editor.get_line(0)
            assert any(span.start == column and span.end == column + 1 for span in line.spans)
            assert app.focused is workspace.command_input
            editor._set_selections(Selection.cursor((1, 300)), [])
            await pilot.pause()
            assert len(editor.get_line(1)) >= 301
            assert editor.text.endswith("y" * 300)
            assert len(ruler.render().spans) == 1

    asyncio.run(exercise())


def test_delete_command_uses_editor_selections_and_undo() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            editor = app.active_workspace.editor
            command = app.active_workspace.command_input
            editor.text = "abc\nabc"
            editor.cursor_location = (0, 1)
            editor.add_cursors(1, 1)
            command.focus()
            await pilot.pause()
            app._handle_command(["del"])
            assert editor.text == "ac\nac"
            assert app.focused is command
            editor.action_undo()
            assert editor.text == "abc\nabc"
            editor._set_selections(Selection((0, 1), (1, 1)), [])
            app._handle_command(["del"])
            assert editor.text == "abc"
            editor.cursor_location = (0, 3)
            app._handle_command(["del"])
            assert editor.text == "abc"
            app._handle_command(["del", "extra"])
            assert "Usage: del" in str(app.active_workspace.result_message.render())
            assert editor.text == "abc"

    asyncio.run(exercise())
