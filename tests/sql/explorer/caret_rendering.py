from __future__ import annotations

import asyncio
from copy import copy

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from rich.segment import Segment
from rich.style import Style
from textual.document._document import Selection
from textual.strip import Strip
from textual.widgets import TextArea

from tests.sql._support.explorer import FakeSession


def test_first_sql_character_and_all_find_matches_survive_carets_and_selection() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 40)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "SELECT order_id FROM orders WHERE order_date > 0"
            editor._set_selections(Selection.cursor((0, 0)), [Selection.cursor((0, 7))])
            editor.set_search_pattern("order")
            await pilot.pause()
            strip = editor.render_line(0)
            assert "SELECT order_id FROM orders WHERE order_date > 0" in strip.text
            assert editor.search_match_count == 3
            for start, end in editor._search_matches:
                match = strip.crop(editor.gutter_width + start[1], editor.gutter_width + end[1])
                assert match.text == "order"
                assert all(
                    segment.style.bgcolor.get_truecolor().hex == "#ffff00" for segment in match
                )
            editor._set_selections(Selection((0, 7), (0, 12)), [])
            await pilot.pause()
            match = editor.render_line(0).crop(editor.gutter_width + 7, editor.gutter_width + 12)
            assert match.text == "order"
            assert all(segment.style.bgcolor.get_truecolor().hex == "#ffff00" for segment in match)

    asyncio.run(exercise())


def test_tabs_wide_characters_and_scrolling_keep_terminal_cells_and_text() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "\t界a" + "x" * 180
            editor._set_selections(Selection.cursor((0, 1)), [Selection.cursor((0, 2))])
            await pilot.pause()
            strip = editor.render_line(0)
            offset, _ = editor.wrapped_document.location_to_offset((0, 1))
            wide = strip.crop(editor.gutter_width + offset, editor.gutter_width + offset + 2)
            assert wide.text == "界"
            assert all(segment.style.underline for segment in wide)
            assert strip.cell_length == editor.scrollable_content_region.width
            editor._set_selections(Selection.cursor((0, 150)), [])
            await pilot.pause()
            assert editor.scroll_x > 0
            offset, _ = editor.wrapped_document.location_to_offset(editor.cursor_location)
            x = editor.gutter_width + offset - int(editor.scroll_x)
            caret = editor.render_line(0).crop(x, x + 1)
            assert caret.text == "x"
            assert all(segment.style.underline for segment in caret)
            assert editor.text == "\t界a" + "x" * 180

    asyncio.run(exercise())


def test_three_line_find_scene_highlights_active_row_in_widget_and_compositor() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(208, 47)) as pilot:
            editor = app.active_workspace.editor
            editor.text = (
                "SELECT order_id, customer_name, revenue\n"
                "FROM analytics.daily_orders\n"
                "WHERE order_date >= CURRENT_DATE - 30"
            )
            app.action_open_find()
            editor.set_search_pattern("order")
            editor._set_selections(Selection.cursor((0, 0)), [Selection.cursor((2, 6))])
            await pilot.pause()
            assert app.focused.id == "find-pattern"
            assert editor.search_match_count == 3
            composed = app.screen._compositor.render_strips()
            viewport = editor.scrollable_content_region
            for start, end in editor._search_matches:
                row, column = start
                left = editor.gutter_width + column
                widget_match = editor.render_line(row).crop(left, left + end[1] - column)
                composed_match = composed[viewport.y + row].crop(
                    viewport.x + left, viewport.x + left + end[1] - column
                )
                for match in (widget_match, composed_match):
                    assert match.text == "order"
                    assert all(
                        segment.style.bgcolor is not None
                        and segment.style.bgcolor.get_truecolor().hex == "#ffff00"
                        for segment in match
                    )
            first = editor.render_line(0).crop(editor.gutter_width, editor.gutter_width + 6)
            assert first.text == "SELECT"
            assert next(iter(first)).style.underline is True
            secondary = editor.render_line(2).crop(editor.gutter_width + 6, editor.gutter_width + 7)
            assert secondary.text == "o"
            assert all(
                segment.style.underline and segment.style.bold is False for segment in secondary
            )

    asyncio.run(exercise())


@pytest.mark.parametrize("command_focus", [False, True])
def test_missing_theme_cursor_style_keeps_blank_and_end_carets_without_document_padding(
    command_focus: bool,
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "SELECT\n"
            editor._theme = copy(editor._theme)
            editor._theme.cursor_style = None
            editor._theme.selection_style = None
            editor._set_selections(Selection.cursor((0, 6)), [Selection.cursor((1, 0))])
            if command_focus:
                app.active_workspace.command_input.focus()
            else:
                editor.focus()
            await pilot.pause()
            for row, column in ((0, 6), (1, 0)):
                line = editor.get_line(row)
                assert len(line) == column + 1
                assert any(span.start == column and span.end == column + 1 for span in line.spans)
                strip = editor.render_line(row)
                assert strip.cell_length == editor.scrollable_content_region.width
                caret = strip.crop(editor.gutter_width + column, editor.gutter_width + column + 1)
                assert caret.text == "│"
                assert all(
                    segment.style.bold is False and not segment.style.reverse for segment in caret
                )
            assert editor.text == "SELECT\n"

    asyncio.run(exercise())


def test_bar_on_a_blank_search_cell_preserves_the_complete_highlight() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "x x"
            editor._set_selections(Selection.cursor((0, 1)), [])
            editor.set_search_pattern("x x")
            app.active_workspace.command_input.focus()
            await pilot.pause()
            viewport = editor.scrollable_content_region
            left = editor.gutter_width
            composed = app.screen._compositor.render_strips()[viewport.y].crop(
                viewport.x + left, viewport.x + left + 3
            )
            for match in (editor.render_line(0).crop(left, left + 3), composed):
                assert match.text == "x│x"
                assert all(
                    segment.style.bgcolor.get_truecolor().hex == "#ffff00" for segment in match
                )
            assert editor.text == "x x"

    asyncio.run(exercise())


@pytest.mark.parametrize("background", [None, "#123456"])
def test_occupied_carets_preserve_original_cell_colors_and_restore_theme(
    background: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)):
            editor = app.active_workspace.editor
            editor.text = "SELECT"
            editor._set_selections(Selection.cursor((0, 0)), [Selection.cursor((0, 2))])
            original = Style(color="#abcdef", bgcolor=background)
            width = editor.scrollable_content_region.width
            segments = [Segment(" " * editor.gutter_width), Segment("SELECT", original)]
            source = Strip(segments).adjust_cell_length(width)
            native_render = TextArea.render_line
            monkeypatch.setattr(TextArea, "render_line", lambda _editor, _row: source)
            theme = editor._theme
            cursor_style = theme.cursor_style
            for focused in (True, False):
                if focused:
                    editor.focus()
                else:
                    app.active_workspace.command_input.focus()
                strip = editor.render_line(0)
                assert strip.text == source.text
                assert strip.cell_length == source.cell_length
                for column in (0, 2):
                    left = editor.gutter_width + column
                    cell = strip.crop(left, left + 1)
                    assert all(segment.style.color == original.color for segment in cell)
                    assert all(segment.style.bgcolor == original.bgcolor for segment in cell)
                    assert all(segment.style.underline is True for segment in cell)
                    assert all(segment.style.bold is False for segment in cell)
                assert editor._theme is theme
                assert theme.cursor_style is cursor_style

            def fail_render(_editor: TextArea, _row: int) -> Strip:
                message = "native render failed"
                raise RuntimeError(message)

            monkeypatch.setattr(TextArea, "render_line", fail_render)
            with pytest.raises(RuntimeError, match="native render failed"):
                editor.render_line(0)
            assert editor._theme is theme
            assert theme.cursor_style is cursor_style
            monkeypatch.setattr(TextArea, "render_line", native_render)
            assert editor.text == "SELECT"

    asyncio.run(exercise())


@pytest.mark.parametrize("background", [None, "#123456"])
def test_blank_and_end_caret_bars_preserve_original_cell_background(
    background: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)):
            editor = app.active_workspace.editor
            editor.text = "x\n"
            editor._set_selections(Selection.cursor((0, 1)), [Selection.cursor((1, 0))])
            original = Style(
                color="#abcdef", bgcolor=background, bold=True, underline=True, reverse=True
            )
            width = editor.scrollable_content_region.width

            def native_line(_editor: TextArea, row: int) -> Strip:
                return Strip(
                    [
                        Segment(" " * editor.gutter_width),
                        Segment("x " if row == 0 else " ", original),
                    ]
                ).adjust_cell_length(width)

            monkeypatch.setattr(TextArea, "render_line", native_line)
            for focused in (True, False):
                if focused:
                    editor.focus()
                else:
                    app.active_workspace.command_input.focus()
                for row, column in ((0, 1), (1, 0)):
                    left = editor.gutter_width + column
                    caret = editor.render_line(row).crop(left, left + 1)
                    assert caret.text == "│"
                    assert caret.cell_length == 1
                    assert all(segment.style.bgcolor == original.bgcolor for segment in caret)
                    assert all(
                        segment.style.color.get_truecolor().hex
                        == app.get_css_variables()["accent"].lower()
                        and segment.style.bold is False
                        and segment.style.underline is False
                        and segment.style.reverse is False
                        for segment in caret
                    )
            assert editor.text == "x\n"

    asyncio.run(exercise())
