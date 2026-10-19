from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.editor import SqlEditor
from analytics_toolkit.sql_explorer.movement import move_command
from analytics_toolkit.sql_explorer.ruler import ColumnRuler, column_numbers, ruler_labels
from textual.document._document import Selection

from tests.sql._support.explorer import FakeSession


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
            selections = editor.cursor_selections
            active = editor.selection
            editor._set_selections(Selection.cursor((0, 250)), [])
            await pilot.pause()
            left = active_x - editor.gutter.left
            original_cell = editor.render_line(0).crop(left, left + 1)
            assert original_cell.text == "x"
            original = next(iter(original_cell)).style
            editor._set_selections(active, [item for item in selections if item != active])
            await pilot.pause()
            caret = editor.render_line(0).crop(left, left + 1)
            assert caret.text == "x"
            assert all(
                segment.text == "x"
                and segment.style.color == original.color
                and segment.style.bgcolor == original.bgcolor
                and segment.style.underline is True
                and segment.style.bold is False
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


def test_tab_database_switch_keeps_ruler_above_separate_gutter() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(208, 47)) as pilot:
            first = app.active_workspace
            first.editor.text = "SELECT 1;\nSELECT 2;"
            first.editor.cursor_location = (1, 4)
            first.command_input.focus()
            await pilot.pause()
            await pilot.press("ctrl+t")
            await pilot.pause()
            second = app.active_workspace
            assert second is not first
            app._command_database(["warehouse_connection"])
            second.command_input.focus()
            await pilot.pause()

            for workspace in (second, first, second):
                app._activate_tab(workspace.tab_id)
                await pilot.pause()
                editor = workspace.editor
                empty = workspace is second
                assert app.active_workspace is workspace
                assert editor.text == ("" if empty else "SELECT 1;\nSELECT 2;")
                assert editor.cursor_location == ((0, 0) if empty else (1, 4))
                _assert_tab_ruler_rows(app)
                assert editor.scroll_x == editor.scroll_y == 0
                assert app.focused is workspace.command_input
                position = "SQL  Ln 1, Col 1" if empty else "SQL  Ln 2, Col 5"
                assert position in str(workspace.query_one("#editor-status").render())
                assert workspace.session.database.connection_key == (
                    "warehouse_connection" if empty else "gp"
                )
                if empty:
                    assert str(workspace.query_one("#notice").renderable) == (
                        "Switched to warehouse_connection (trino)."
                    )
                    assert workspace.query_one(ColumnRuler).render().plain.strip() == "1"
            assert first.editor.text == "SELECT 1;\nSELECT 2;"
            assert second.editor.text == ""

    asyncio.run(exercise())


def _assert_tab_ruler_rows(app: SqlExplorerApp) -> None:
    editor = app.active_workspace.editor
    ruler = app.active_workspace.query_one(ColumnRuler)
    assert ruler.region.height == 1
    assert ruler.region.bottom == editor.region.y
    assert ruler.region.x == editor.region.x
    text_x = editor.content_region.x + editor.gutter_width
    caret_x = text_x + editor.cursor_render_offset[0]
    ruler_x = caret_x - ruler.content_region.x
    label = str(editor.cursor_location[1] + 1)
    assert ruler.render_line(0).crop(ruler_x, ruler_x + 1).text == label
    rows = app.screen._compositor.render_strips()
    assert rows[ruler.content_region.y].crop(caret_x, caret_x + 1).text == label
    first_row = editor.render_line(0)
    composed = rows[editor.content_region.y]
    assert first_row.crop(0, editor.gutter_width).text.strip() == "1"
    assert composed.crop(editor.content_region.x, text_x).text.strip() == "1"
    visible_text = "SELECT 1;" if editor.text else "│"
    text_end = editor.gutter_width + len(visible_text)
    assert first_row.crop(editor.gutter_width, text_end).text == visible_text
    assert composed.crop(text_x, text_x + len(visible_text)).text == visible_text


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


def test_horizontal_scrollbar_tracks_offset_extents_resize_and_shorter_document() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 40)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "SELECT " + "x" * 500
            app.active_workspace.command_input.focus()
            await pilot.pause()
            scrollbar = editor.horizontal_scrollbar
            painted = []
            composed = []
            for x in (0, editor.max_scroll_x // 2, editor.max_scroll_x):
                editor.scroll_to(x=x, animate=False, force=True)
                await pilot.pause()
                assert scrollbar.position == editor.scroll_x
                assert scrollbar.window_virtual_size == editor.virtual_size.width
                assert scrollbar.window_size == editor.scrollable_content_region.width
                assert scrollbar.window_virtual_size - scrollbar.window_size == editor.max_scroll_x
                painted.append(
                    [(segment.text, str(segment.style)) for segment in scrollbar.render_line(0)]
                )
                region = scrollbar.region
                screen_strip = app.screen._compositor.render_strips()[region.y].crop(
                    region.x, region.right
                )
                composed.append([(segment.text, str(segment.style)) for segment in screen_strip])
            assert painted[0] != painted[1]
            assert painted[1] != painted[2]
            assert composed[0] != composed[1]
            assert composed[1] != composed[2]
            await pilot.resize_terminal(90, 35)
            await pilot.pause()
            assert scrollbar.position == editor.scroll_x
            assert scrollbar.window_size == editor.scrollable_content_region.width
            editor.text = "SELECT 1"
            await pilot.pause()
            assert editor.scroll_x == scrollbar.position == 0
            assert scrollbar.window_virtual_size == editor.virtual_size.width
            assert editor.max_scroll_x == 0

    asyncio.run(exercise())


@pytest.mark.parametrize(("offset", "cell"), [(0.0, 0), (12.75, 12), (48.0, 48)])
def test_horizontal_scrollbar_uses_the_rendered_integer_cell(offset: float, cell: int) -> None:
    scrollbar = SimpleNamespace(position=0, window_size=0, window_virtual_size=0)
    editor = SimpleNamespace(
        is_mounted=True,
        horizontal_scrollbar=scrollbar,
        scroll_x=offset,
        virtual_size=SimpleNamespace(width=300),
        scrollable_content_region=SimpleNamespace(width=80),
    )
    SqlEditor._sync_horizontal_scrollbar(editor)
    assert scrollbar.position == cell
    assert isinstance(scrollbar.position, int)
    assert scrollbar.window_size == 80
    assert scrollbar.window_virtual_size == 300


def test_unmounted_editor_does_not_access_scrollbar_controls() -> None:
    SqlEditor._sync_horizontal_scrollbar(SimpleNamespace(is_mounted=False))
