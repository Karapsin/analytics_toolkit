from __future__ import annotations

import asyncio
from copy import copy

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.editor import CARET_COMPONENT
from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from textual.document._document import Selection
from textual.strip import Strip
from textual.widgets import TextArea

from tests.sql._support.carets import (
    assert_caret_block,
    assert_no_caret_cells,
    caret_cells,
    input_cursor_style,
)
from tests.sql._support.explorer import FakeSession


def test_editor_carets_use_the_command_pane_input_cursor_style() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            workspace = app.active_workspace
            editor = workspace.editor
            editor.text = "SELECT"
            editor._set_selections(Selection.cursor((0, 0)), [Selection.cursor((0, 6))])
            await pilot.pause()
            cursor = input_cursor_style(app)
            assert editor.get_component_rich_style(CARET_COMPONENT) == cursor
            # The command pane draws exactly this style on the cell at its cursor.
            workspace.command_input.value = "run"
            workspace.command_input.focus()
            await pilot.pause()
            workspace.command_input.cursor_position = 0
            await pilot.pause()
            command_row = workspace.command_input.render_line(0)
            command_cursor = next(
                segment for segment in command_row if segment.text.startswith("r")
            ).style
            assert command_cursor.color == cursor.color
            assert command_cursor.bgcolor == cursor.bgcolor
            assert bool(command_cursor.reverse) == bool(cursor.reverse)
            strip = editor.render_line(0)
            gutter = editor.gutter_width
            assert_caret_block(strip, gutter, cursor, text="S")
            assert_caret_block(strip, gutter + 6, cursor, text=" ")
            assert strip.crop(gutter, gutter + 7).text == "SELECT "

    asyncio.run(exercise())


@pytest.mark.parametrize("command_focus", [False, True])
def test_blocks_cover_the_character_cell_in_every_caret_position(command_focus: bool) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "SELECT a\n\nxyz"
            editor._set_selections(
                Selection.cursor((0, 3)),
                [Selection.cursor((0, 8)), Selection.cursor((1, 0)), Selection.cursor((2, 1))],
            )
            if command_focus:
                app.active_workspace.command_input.focus()
            else:
                editor.focus()
            await pilot.pause()
            cursor = input_cursor_style(app)
            viewport = editor.scrollable_content_region
            composed = app.screen._compositor.render_strips()
            gutter = editor.gutter_width
            expected = {0: ("SELECT a ", [3, 8]), 1: (" ", [0]), 2: ("xyz", [1])}
            for row, (text, columns) in expected.items():
                screen = composed[viewport.y + row].crop(viewport.x, viewport.right)
                cells = [gutter + column for column in columns]
                assert caret_cells(editor, row) == cells
                for strip in (editor.render_line(row), screen):
                    assert strip.cell_length == viewport.width
                    assert strip.crop(gutter, gutter + len(text)).text == text
                    for x in cells:
                        assert_caret_block(strip, x, cursor, text=(text + " ")[x - gutter])
                    assert_no_caret_cells(strip, cursor, cells)
                assert editor.get_line(row).plain == editor.document[row]
            assert editor.text == "SELECT a\n\nxyz"

    asyncio.run(exercise())


def test_find_matches_and_selections_survive_block_carets() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 40)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "SELECT order_id FROM orders WHERE order_date > 0"
            editor._set_selections(Selection.cursor((0, 0)), [Selection.cursor((0, 9))])
            editor.set_search_pattern("order")
            await pilot.pause()
            cursor = input_cursor_style(app)
            gutter = editor.gutter_width
            strip = editor.render_line(0)
            assert caret_cells(editor, 0) == [gutter, gutter + 9]
            assert "SELECT order_id FROM orders WHERE order_date > 0" in strip.text
            assert_caret_block(strip, gutter, cursor, text="S")
            # A caret inside a find match is the same block; the rest of the match stays yellow.
            assert_caret_block(strip, gutter + 9, cursor, text="d")
            assert editor.search_match_count == 3
            for start, end in editor._search_matches:
                for column in range(start[1], end[1]):
                    if column == 9:
                        continue
                    cell = strip.crop(gutter + column, gutter + column + 1)
                    assert all(
                        segment.style.bgcolor.get_truecolor().hex == "#ffff00" for segment in cell
                    )
            for selection in (Selection((0, 0), (0, 6)), Selection((0, 6), (0, 0))):
                editor._set_selections(selection, [])
                await pilot.pause()
                strip = editor.render_line(0)
                [x] = caret_cells(editor, 0)
                assert x == gutter + selection.end[1]
                assert_caret_block(strip, x, cursor)
                assert strip.crop(gutter, gutter + 6).text == "SELECT"

    asyncio.run(exercise())


def test_tabs_wide_characters_and_scrolling_keep_every_character_in_place() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "\t界a" + "x" * 180
            editor._set_selections(Selection.cursor((0, 0)), [Selection.cursor((0, 1))])
            await pilot.pause()
            cursor = input_cursor_style(app)
            strip = editor.render_line(0)
            gutter = editor.gutter_width
            tab = " " * editor.indent_width
            assert strip.crop(gutter, gutter + len(tab) + 3).text == tab + "界a"
            # Expanded tabs and wide characters are covered across all of their cells.
            assert_caret_block(strip, gutter, cursor, text=tab, width=len(tab))
            assert_caret_block(strip, gutter + len(tab), cursor, text="界", width=2)
            assert strip.cell_length == editor.scrollable_content_region.width
            editor._set_selections(Selection.cursor((0, 150)), [])
            await pilot.pause()
            assert editor.scroll_x > 0
            [x] = caret_cells(editor, 0)
            strip = editor.render_line(0)
            assert_caret_block(strip, x, cursor, text="x")
            assert strip.crop(x - 1, x + 2).text == "xxx"
            assert editor.text == "\t界a" + "x" * 180

    asyncio.run(exercise())


def test_horizontally_clipped_tabs_keep_their_visible_caret_cells() -> None:
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
            cursor = input_cursor_style(app)
            viewport = editor.scrollable_content_region
            composed = app.screen._compositor.render_strips()
            visible = editor.indent_width - 1
            for row in range(2):
                for strip in (
                    editor.render_line(row),
                    composed[viewport.y + row].crop(viewport.x, viewport.right),
                ):
                    assert_caret_block(
                        strip, editor.gutter_width, cursor, text=" " * visible, width=visible
                    )
                    assert strip.crop(
                        editor.gutter_width + visible, editor.gutter_width + 10
                    ).text == "z" * (10 - visible)
                    assert strip.cell_length == viewport.width
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
            cursor = input_cursor_style(app)
            viewport = editor.scrollable_content_region
            composed = app.screen._compositor.render_strips()
            for row in range(2):
                offset, visual_row = editor.wrapped_document.location_to_offset((row, 0))
                _, next_row = editor.wrapped_document.location_to_offset((row, 1))
                assert offset == 0
                assert next_row > visual_row
                for strip in (
                    editor.render_line(visual_row),
                    composed[viewport.y + visual_row].crop(viewport.x, viewport.right),
                ):
                    width = viewport.width - editor.gutter_width
                    assert width == editor.indent_width
                    assert_caret_block(
                        strip, editor.gutter_width, cursor, text=" " * width, width=width
                    )
                assert editor.get_line(row).plain == "\tZ"
            assert editor.text == "\tZ\n\tZ"

    asyncio.run(exercise())


def test_soft_wrapped_rows_draw_blocks_on_the_caret_visual_row() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.soft_wrap = True
            editor.show_line_numbers = False
            await pilot.pause()
            text = "".join(chr(ord("a") + index % 26) for index in range(300))
            editor.text = text
            wrap_width = editor.wrap_width
            carets = [Selection.cursor((0, column)) for column in (1, 2, 3, wrap_width + 1)]
            editor._set_selections(Selection.cursor((0, wrap_width - 1)), carets)
            await pilot.pause()
            # The main caret is on the last character before a soft wrap.
            assert editor.wrapped_document.location_to_offset((0, wrap_width))[1] == 1
            cursor = input_cursor_style(app)
            shown = ""
            blocks = 0
            for row in range(editor.wrapped_document.height):
                viewport_row = row - int(editor.scroll_y)
                strip = editor.render_line(viewport_row)
                cells = caret_cells(editor, viewport_row)
                for x in cells:
                    assert_caret_block(strip, x, cursor)
                assert_no_caret_cells(strip, cursor, cells)
                blocks += len(cells)
                shown += strip.text.rstrip()
            assert blocks == 5
            # No cell is inserted, so soft wrapping and the text are unchanged.
            assert shown == text

    asyncio.run(exercise())


@pytest.mark.parametrize("command_focus", [False, True])
def test_missing_theme_styles_keep_blocks_without_document_padding(command_focus: bool) -> None:
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
            cursor = input_cursor_style(app)
            for row, column in ((0, 6), (1, 0)):
                assert editor.get_line(row).plain == editor.document[row]
                strip = editor.render_line(row)
                assert strip.cell_length == editor.scrollable_content_region.width
                assert caret_cells(editor, row) == [editor.gutter_width + column]
                assert_caret_block(strip, editor.gutter_width + column, cursor, text=" ")
            assert editor.text == "SELECT\n"

    asyncio.run(exercise())


@pytest.mark.parametrize("text", ["SELECT", "S LECT", "S\tLECT"])
@pytest.mark.parametrize("background", [None, "#123456"])
def test_blocks_keep_other_cells_and_restore_theme(
    text: str,
    background: str | None,
    monkeypatch: pytest.MonkeyPatch,
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
            expanded = text.expandtabs(editor.indent_width)
            segments = [Segment(" " * editor.gutter_width), Segment(expanded, original)]
            source = Strip(segments).adjust_cell_length(width)
            native_render = TextArea.render_line
            monkeypatch.setattr(TextArea, "render_line", lambda _editor, _row: source)
            theme = editor._theme
            cursor_style = theme.cursor_style
            theme_state = vars(theme).copy()
            cursor = input_cursor_style(app)
            for focused in (True, False):
                if focused:
                    editor.focus()
                else:
                    app.active_workspace.command_input.focus()
                strip = editor.render_line(0)
                assert strip.cell_length == source.cell_length
                assert strip.text == source.text
                caret_end = editor.wrapped_document.location_to_offset((0, 3))[0]
                assert_caret_block(
                    strip,
                    editor.gutter_width,
                    cursor,
                    text=expanded[:caret_end],
                    width=caret_end,
                )
                rest = strip.crop(
                    editor.gutter_width + caret_end, editor.gutter_width + len(expanded)
                )
                for segment in rest:
                    assert segment.style.color == original.color
                    assert segment.style.bgcolor == original.bgcolor
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
def test_blank_and_end_caret_blocks_use_the_input_cursor_colors(
    background: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)):
            editor = app.active_workspace.editor
            editor.text = "x\n"
            editor._set_selections(Selection.cursor((0, 1)), [Selection.cursor((1, 0))])
            original = Style(color="#abcdef", bgcolor=background, bold=True, underline=True)
            width = editor.scrollable_content_region.width

            def native_line(_editor: TextArea, row: int) -> Strip:
                return Strip(
                    [
                        Segment(" " * editor.gutter_width),
                        Segment("x " if row == 0 else " ", original),
                    ]
                ).adjust_cell_length(width)

            monkeypatch.setattr(TextArea, "render_line", native_line)
            cursor = input_cursor_style(app)
            for focused in (True, False):
                if focused:
                    editor.focus()
                else:
                    app.active_workspace.command_input.focus()
                for row, column in ((0, 1), (1, 0)):
                    left = editor.gutter_width + column
                    assert_caret_block(editor.render_line(row), left, cursor, text=" ")
            assert editor.text == "x\n"

    asyncio.run(exercise())


def test_clicking_a_character_places_the_caret_on_it() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "SELECT value\nFROM t"
            editor._set_selections(Selection.cursor((0, 3)), [Selection.cursor((0, 8))])
            await pilot.pause()
            # Carets occupy no extra cells, so the clicked cell is the document column.
            offset_x = editor.content_region.x - editor.region.x + editor.gutter_width + 10
            offset_y = editor.content_region.y - editor.region.y
            await pilot.click("#query-editor", offset=(offset_x, offset_y))
            await pilot.pause()
            assert editor.cursor_location == (0, 10)
            assert editor.cursor_count == 1
            [x] = caret_cells(editor, 0)
            assert_caret_block(editor.render_line(0), x, input_cursor_style(app), text="u")

    asyncio.run(exercise())


@pytest.mark.parametrize("command_focus", [False, True])
@pytest.mark.parametrize("column", [0, 10])
def test_native_renderer_preserves_syntax_colors_beside_blocks(
    command_focus: bool,
    column: int,
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
            if command_focus:
                app.active_workspace.command_input.focus()
            else:
                editor.focus()
            editor._set_selections(
                Selection.cursor((0, len(line))), [Selection.cursor((1, len(line)))]
            )
            await pilot.pause()
            theme = editor._theme
            theme_state = vars(theme).copy()
            baseline = [editor.render_line(row) for row in range(2)]
            editor._set_selections(Selection.cursor((0, column)), [Selection.cursor((1, column))])
            await pilot.pause()
            viewport = editor.scrollable_content_region
            cursor = input_cursor_style(app)
            left = editor.gutter_width + column
            composed = app.screen._compositor.render_strips()
            for row in range(2):
                for strip in (
                    editor.render_line(row),
                    composed[viewport.y + row].crop(viewport.x, viewport.right),
                ):
                    assert strip.cell_length == viewport.width
                    assert_caret_block(strip, left, cursor, text=line[column])
                    assert strip.crop(
                        editor.gutter_width, editor.gutter_width + len(line)
                    ).text == (line)
                    following = next(iter(strip.crop(left + 1, left + 2))).style
                    original = next(iter(baseline[row].crop(left + 1, left + 2))).style
                    assert following.color == original.color
                    if column == 0:
                        assert following.color == Style(color="#12ab34").color
            assert editor._theme is theme
            assert vars(theme) == theme_state
            assert editor.text == line + "\n" + line

    asyncio.run(exercise())


def test_end_caret_inside_stale_search_match_renders_a_block_before_refresh() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 40)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "SELECT order"
            editor._set_selections(Selection.cursor((0, 12)), [])
            editor.set_search_pattern("order")
            await pilot.pause()
            assert editor._search_matches == (((0, 7), (0, 12)),)

            # Render before TextArea.Changed refreshes the matches: the shortened
            # line leaves the end caret inside the previous match.
            editor.delete((0, 11), (0, 12))
            assert editor.document[0] == "SELECT orde"
            assert editor.selection.end == (0, 11)
            assert editor._search_matches == (((0, 7), (0, 12)),)
            strip = editor.render_line(0)
            gutter = editor.gutter_width
            assert strip.crop(gutter, gutter + 12).text == "SELECT orde "
            assert_caret_block(strip, gutter + 11, input_cursor_style(app), text=" ")
            match = strip.crop(gutter + 7, gutter + 11)
            assert all(segment.style.bgcolor.get_truecolor().hex == "#ffff00" for segment in match)

            await pilot.pause()
            assert editor._search_matches == ()

    asyncio.run(exercise())


def test_main_caret_at_the_far_end_of_a_long_row_stays_visible() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "x" * 300
            secondary = [Selection.cursor((0, column)) for column in (250, 260, 270, 280)]
            editor._set_selections(Selection.cursor((0, 300)), secondary)
            await pilot.pause()
            width = editor.scrollable_content_region.width
            cells = caret_cells(editor, 0)
            assert len(cells) == 5
            assert cells[-1] < width
            strip = editor.render_line(0)
            cursor = input_cursor_style(app)
            for x in cells:
                assert_caret_block(strip, x, cursor)
            assert strip.crop(cells[0], cells[-1]).text == "x" * (cells[-1] - cells[0])

    asyncio.run(exercise())


def test_secondary_caret_trailing_the_document_is_skipped_while_rendering() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "SELECT"
            editor._set_selections(Selection.cursor((0, 1)), [])
            await pilot.pause()
            # Mid batch edit, a secondary caret can briefly point past the last line.
            editor._secondary_selections = [Selection.cursor((5, 0))]
            strip = editor.render_line(0)
            cursor = input_cursor_style(app)
            gutter = editor.gutter_width
            assert strip.crop(gutter, gutter + 6).text == "SELECT"
            assert_caret_block(strip, gutter + 1, cursor, text="E")
            assert_no_caret_cells(strip, cursor, [gutter + 1])

    asyncio.run(exercise())


@pytest.mark.parametrize("character", ["x", "界", "\t"])
def test_caret_before_a_soft_wrap_covers_only_its_character(character: str) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.soft_wrap = True
            editor.show_line_numbers = False
            await pilot.pause()
            wrap_width = editor.wrap_width
            # A tab stop that divides the row lets the expanded tab end it exactly.
            editor.indent_width = next(n for n in range(2, wrap_width + 1) if wrap_width % n == 0)
            cells = editor.indent_width if character == "\t" else cell_len(character)
            filler = "y" * (wrap_width - cells)
            editor.text = filler + character + "z" * 20
            column = len(filler)
            editor._set_selections(Selection.cursor((0, column)), [])
            await pilot.pause()
            x, visual_row = editor.wrapped_document.location_to_offset((0, column))
            assert x + cells == wrap_width
            assert editor.wrapped_document.location_to_offset((0, column + 1)) == (0, 1)
            strip = editor.render_line(visual_row - int(editor.scroll_y))
            left = x - int(editor.scroll_x) + editor.gutter_width
            cursor = input_cursor_style(app)
            assert_caret_block(strip, left, cursor, width=cells)
            # The padding after the character keeps the editor background.
            assert_no_caret_cells(strip, cursor, list(range(left, left + cells)))

    asyncio.run(exercise())
