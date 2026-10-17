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


def test_underlined_search_space_preserves_the_complete_highlight() -> None:
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
                assert match.text == "x x"
                assert all(segment.style.underline for segment in match.crop(1, 2))
                assert all(
                    segment.style.bgcolor.get_truecolor().hex == "#ffff00" for segment in match
                )
            assert editor.text == "x x"

    asyncio.run(exercise())


@pytest.mark.parametrize("text", ["SELECT", "S LECT", "S\tLECT"])
@pytest.mark.parametrize("background", [None, "#123456"])
def test_occupied_carets_preserve_original_cell_colors_and_restore_theme(
    text: str, background: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)):
            editor = app.active_workspace.editor
            editor.text = text
            editor._set_selections(
                Selection.cursor((0, 0)), [Selection.cursor((0, 1)), Selection.cursor((0, 2))]
            )
            original = Style(color="#abcdef", bgcolor=background)
            width = editor.scrollable_content_region.width
            segments = [
                Segment(" " * editor.gutter_width),
                Segment(text.expandtabs(editor.indent_width), original),
            ]
            source = Strip(segments).adjust_cell_length(width)
            native_render = TextArea.render_line
            monkeypatch.setattr(TextArea, "render_line", lambda _editor, _row: source)
            theme = editor._theme
            cursor_style = theme.cursor_style
            theme_state = vars(theme).copy()
            for focused in (True, False):
                if focused:
                    editor.focus()
                else:
                    app.active_workspace.command_input.focus()
                strip = editor.render_line(0)
                assert strip.text == source.text
                assert strip.cell_length == source.cell_length
                for column in (0, 1, 2):
                    offset, _ = editor.wrapped_document.location_to_offset((0, column))
                    left = editor.gutter_width + offset
                    cell = strip.crop(left, left + 1)
                    assert all(segment.style.color == original.color for segment in cell)
                    assert all(segment.style.bgcolor == original.bgcolor for segment in cell)
                    assert all(segment.style.underline is True for segment in cell)
                    assert all(segment.style.bold is False for segment in cell)
                assert editor._theme is theme
                assert theme.cursor_style is cursor_style
                assert vars(theme) == theme_state

            def fail_render(_editor: TextArea, _row: int) -> Strip:
                native_render(_editor, _row)
                assert _editor._theme.cursor_style == Style()
                message = "native render failed"
                raise RuntimeError(message)

            monkeypatch.setattr(TextArea, "render_line", fail_render)
            with pytest.raises(RuntimeError, match="native render failed"):
                editor.render_line(0)
            assert editor._theme is theme
            assert theme.cursor_style is cursor_style
            assert vars(theme) == theme_state
            monkeypatch.setattr(TextArea, "render_line", native_render)
            assert editor.text == text

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


@pytest.mark.parametrize("command_focus", [False, True])
@pytest.mark.parametrize("line", ["x x", "x\tx", "\t\tx"])
def test_whitespace_carets_preserve_widget_and_compositor_cells(
    line: str, command_focus: bool
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = line + "\n" + line
            editor._set_selections(Selection.cursor((0, 2)), [Selection.cursor((1, 2))])
            if command_focus:
                app.active_workspace.command_input.focus()
            else:
                editor.focus()
            await pilot.pause()
            baseline = [editor.render_line(row) for row in range(2)]
            editor._set_selections(Selection.cursor((0, 1)), [Selection.cursor((1, 1))])
            await pilot.pause()
            viewport = editor.scrollable_content_region
            composed = app.screen._compositor.render_strips()
            expanded = line.expandtabs(editor.indent_width)
            for row in range(2):
                offset, visual_row = editor.wrapped_document.location_to_offset((row, 1))
                next_offset, _ = editor.wrapped_document.location_to_offset((row, 2))
                left = editor.gutter_width + offset
                right = editor.gutter_width + next_offset
                original = baseline[row].crop(left, right)
                widget = editor.render_line(visual_row)
                assert widget.cell_length == viewport.width
                visible_line = widget.crop(editor.gutter_width, editor.gutter_width + len(expanded))
                assert visible_line.text == expanded
                screen = composed[viewport.y + visual_row].crop(viewport.x, viewport.right)
                for strip in (widget, screen):
                    caret = strip.crop(left, right)
                    assert caret.text == original.text == " " * (next_offset - offset)
                    assert caret.cell_length == original.cell_length
                    original_style = next(iter(original)).style
                    assert all(
                        segment.style.color == original_style.color
                        and segment.style.bgcolor == original_style.bgcolor
                        and segment.style.underline is True
                        and segment.style.bold is False
                        and segment.style.reverse is False
                        for segment in caret
                    )
                rendered_line = editor.get_line(row)
                assert rendered_line.plain == line
                assert rendered_line.get_style_at_offset(app.console, 1).underline is True
            assert editor.text == line + "\n" + line

    asyncio.run(exercise())


@pytest.mark.parametrize("command_focus", [False, True])
def test_tab_carets_fill_the_visible_cells_when_the_following_character_wraps(
    command_focus: bool,
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.soft_wrap = True
            editor.show_line_numbers = False
            await pilot.pause()
            editor.indent_width = editor.scrollable_content_region.width - editor.gutter_width
            editor.text = "\tZ\n\tZ"
            editor._set_selections(Selection.cursor((0, 0)), [Selection.cursor((1, 0))])
            if command_focus:
                app.active_workspace.command_input.focus()
            else:
                editor.focus()
            await pilot.pause()
            viewport = editor.scrollable_content_region
            composed = app.screen._compositor.render_strips()
            for row in range(2):
                offset, visual_row = editor.wrapped_document.location_to_offset((row, 0))
                _, next_row = editor.wrapped_document.location_to_offset((row, 1))
                assert offset == 0
                assert next_row > visual_row
                left = editor.gutter_width
                for strip in (
                    editor.render_line(visual_row),
                    composed[viewport.y + visual_row].crop(viewport.x, viewport.right),
                ):
                    caret = strip.crop(left, viewport.width)
                    assert caret.cell_length == editor.indent_width
                    assert caret.text == " " * editor.indent_width
                    assert all(
                        segment.style.underline is True
                        and segment.style.bold is False
                        and segment.style.reverse is False
                        for segment in caret
                    )
                assert editor.get_line(row).plain == "\tZ"
            assert editor.text == "\tZ\n\tZ"

    asyncio.run(exercise())


def test_horizontally_clipped_tabs_keep_all_visible_caret_cells() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            line = "x" * 100 + "\t" + "z" * 200
            editor.text = line + "\n" + line
            editor._set_selections(Selection.cursor((0, 100)), [Selection.cursor((1, 100))])
            await pilot.pause()
            offset, _ = editor.wrapped_document.location_to_offset((0, 100))
            editor.scroll_to(x=offset + 1, animate=False, force=True)
            await pilot.pause()
            assert int(editor.scroll_x) == offset + 1
            viewport = editor.scrollable_content_region
            composed = app.screen._compositor.render_strips()
            for row in range(2):
                for strip in (
                    editor.render_line(row),
                    composed[viewport.y + row].crop(viewport.x, viewport.right),
                ):
                    caret = strip.crop(
                        editor.gutter_width, editor.gutter_width + editor.indent_width - 1
                    )
                    assert caret.text == " " * (editor.indent_width - 1)
                    assert all(segment.style.underline is True for segment in caret)
                    assert strip.cell_length == viewport.width
            assert editor.text == line + "\n" + line

    asyncio.run(exercise())


@pytest.mark.parametrize("command_focus", [False, True])
@pytest.mark.parametrize("column", [0, 10])
def test_native_renderer_preserves_syntax_and_completion_separator_colors(
    command_focus: bool, column: int
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            line = "SELECT c.c FROM"
            editor.text = line + "\n" + line
            editor._theme = copy(editor._theme)
            editor._theme.syntax_styles = {
                **editor._theme.syntax_styles,
                "keyword": Style(color="#12ab34"),
            }
            for row in range(2):
                editor._highlights[row] = [(0, 6, "keyword")]
            editor._set_selections(
                Selection.cursor((0, len(line))), [Selection.cursor((1, len(line)))]
            )
            if command_focus:
                app.active_workspace.command_input.focus()
            else:
                editor.focus()
            await pilot.pause()
            theme = editor._theme
            theme_state = vars(theme).copy()
            baseline = [editor.render_line(row) for row in range(2)]
            editor._set_selections(
                Selection.cursor((0, column)), [Selection.cursor((1, column))]
            )
            await pilot.pause()
            viewport = editor.scrollable_content_region
            left = editor.gutter_width + column
            for _ in range(2):
                composed = app.screen._compositor.render_strips()
                for row in range(2):
                    for strip in (
                        editor.render_line(row),
                        composed[viewport.y + row].crop(viewport.x, viewport.right),
                    ):
                        assert strip.cell_length == viewport.width
                        assert (
                            strip.crop(editor.gutter_width, editor.gutter_width + len(line)).text
                            == line
                        )
                        caret = strip.crop(left, left + 1)
                        assert caret.text == baseline[row].crop(left, left + 1).text == line[column]
                        original_style = next(iter(baseline[row].crop(left, left + 1))).style
                        assert all(
                            segment.style.color == original_style.color
                            and segment.style.bgcolor == original_style.bgcolor
                            and segment.style.underline is True
                            and segment.style.bold is False
                            and segment.style.reverse is False
                            for segment in caret
                        )
                        if column == 0:
                            assert original_style.color == Style(color="#12ab34").color
                assert editor._theme is theme
                assert vars(theme) == theme_state
            assert editor.text == line + "\n" + line

    asyncio.run(exercise())
