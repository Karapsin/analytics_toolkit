from __future__ import annotations

import asyncio
from copy import copy
from types import SimpleNamespace

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from rich.segment import Segment
from rich.style import Style
from textual.document._document import Selection
from textual.strip import Strip
from textual.widgets import TextArea

from tests.sql._support.carets import (
    CARET,
    assert_caret_bar,
    assert_plain_cells,
    caret_cells,
    without_cells,
)
from tests.sql._support.explorer import FakeSession


@pytest.mark.parametrize("command_focus", [False, True])
def test_bars_sit_between_characters_in_every_caret_position(command_focus: bool) -> None:
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
            accent = app.get_css_variables()["accent"]
            viewport = editor.scrollable_content_region
            composed = app.screen._compositor.render_strips()
            gutter = editor.gutter_width
            expected = {0: "SEL▌ECT a▌", 1: "▌", 2: "x▌yz"}
            for row, text in expected.items():
                screen = composed[viewport.y + row].crop(viewport.x, viewport.right)
                for strip in (editor.render_line(row), screen):
                    assert strip.cell_length == viewport.width
                    assert strip.crop(gutter, gutter + len(text)).text == text
                    for x in caret_cells(editor, row):
                        assert_caret_bar(strip, x, accent)
                    assert_plain_cells(without_cells(strip, caret_cells(editor, row)))
                rendered_line = editor.get_line(row)
                assert rendered_line.plain == editor.document[row]
                assert not any(
                    isinstance(span.style, Style) and span.style.underline
                    for span in rendered_line.spans
                )
            assert editor.text == "SELECT a\n\nxyz"

    asyncio.run(exercise())


def test_find_matches_and_selections_survive_bars() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 40)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "SELECT order_id FROM orders WHERE order_date > 0"
            editor._set_selections(Selection.cursor((0, 0)), [Selection.cursor((0, 9))])
            editor.set_search_pattern("order")
            await pilot.pause()
            accent = app.get_css_variables()["accent"]
            strip = editor.render_line(0)
            cells = caret_cells(editor, 0)
            assert cells == [editor.gutter_width, editor.gutter_width + 10]
            assert_caret_bar(strip, cells[0], accent)
            # A bar inside a find match keeps the match background and turns black.
            inside = list(strip.crop(cells[1], cells[1] + 1))
            assert [segment.text for segment in inside] == [CARET]
            assert inside[0].style.bgcolor.get_truecolor().hex == "#ffff00"
            assert inside[0].style.color.get_truecolor().hex == "#000000"
            plain = without_cells(strip, cells)
            assert "SELECT order_id FROM orders WHERE order_date > 0" in plain.text
            assert editor.search_match_count == 3
            for start, end in editor._search_matches:
                match = plain.crop(editor.gutter_width + start[1], editor.gutter_width + end[1])
                assert match.text == "order"
                assert all(
                    segment.style.bgcolor.get_truecolor().hex == "#ffff00" for segment in match
                )
            for selection in (Selection((0, 0), (0, 6)), Selection((0, 6), (0, 0))):
                editor._set_selections(selection, [])
                await pilot.pause()
                strip = editor.render_line(0)
                x = caret_cells(editor, 0)[0]
                following = next(iter(strip.crop(x + 1, x + 2))).style
                assert_caret_bar(strip, x, accent, bgcolor=following.bgcolor)
                assert without_cells(strip, [x]).crop(
                    editor.gutter_width, editor.gutter_width + 6
                ).text == ("SELECT")

    asyncio.run(exercise())


def test_tabs_wide_characters_and_scrolling_keep_every_character_visible() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "\t界a" + "x" * 180
            editor._set_selections(Selection.cursor((0, 1)), [Selection.cursor((0, 2))])
            await pilot.pause()
            accent = app.get_css_variables()["accent"]
            strip = editor.render_line(0)
            gutter = editor.gutter_width
            tab = " " * editor.indent_width
            assert strip.crop(gutter, gutter + len(tab) + 5).text == tab + "▌界▌a"
            for x in caret_cells(editor, 0):
                assert_caret_bar(strip, x, accent)
            assert strip.cell_length == editor.scrollable_content_region.width
            editor._set_selections(Selection.cursor((0, 150)), [])
            await pilot.pause()
            assert editor.scroll_x > 0
            [x] = caret_cells(editor, 0)
            strip = editor.render_line(0)
            assert_caret_bar(strip, x, accent)
            assert strip.crop(x - 1, x + 2).text == "x▌x"
            assert editor.text == "\t界a" + "x" * 180

    asyncio.run(exercise())


def test_horizontally_clipped_carets_leave_the_visible_row_unchanged() -> None:
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
                assert caret_cells(editor, row) == []
                for strip in (
                    editor.render_line(row),
                    composed[viewport.y + row].crop(viewport.x, viewport.right),
                ):
                    assert CARET not in strip.crop(editor.gutter_width, viewport.width).text
                    visible = strip.crop(editor.gutter_width, editor.gutter_width + 10).text
                    assert visible == " " * (editor.indent_width - 1) + "z" * (
                        11 - editor.indent_width
                    )
                    assert strip.cell_length == viewport.width
            assert editor.text == line + "\n" + line

    asyncio.run(exercise())


def test_soft_wrapped_rows_draw_bars_on_the_caret_visual_row() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.soft_wrap = True
            editor.show_line_numbers = False
            await pilot.pause()
            width = editor.scrollable_content_region.width
            editor.text = "a" * width + "bcd"
            editor._set_selections(Selection.cursor((0, width + 1)), [])
            await pilot.pause()
            x, visual_row = editor.wrapped_document.location_to_offset((0, width + 1))
            assert visual_row == 1
            assert CARET not in editor.render_line(0).text
            strip = editor.render_line(1)
            assert strip.crop(x - 1, x + 2).text == "b▌c"
            assert_caret_bar(strip, x, app.get_css_variables()["accent"])
            assert strip.cell_length == width

    asyncio.run(exercise())


@pytest.mark.parametrize("command_focus", [False, True])
def test_missing_theme_styles_keep_bars_without_document_padding(command_focus: bool) -> None:
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
                assert editor.get_line(row).plain == editor.document[row]
                strip = editor.render_line(row)
                assert strip.cell_length == editor.scrollable_content_region.width
                assert caret_cells(editor, row) == [editor.gutter_width + column]
                caret = strip.crop(editor.gutter_width + column, editor.gutter_width + column + 1)
                assert caret.text == CARET
                assert all(
                    segment.style.bold is False and not segment.style.reverse for segment in caret
                )
            assert editor.text == "SELECT\n"

    asyncio.run(exercise())


@pytest.mark.parametrize("text", ["SELECT", "S LECT", "S\tLECT"])
@pytest.mark.parametrize("background", [None, "#123456"])
def test_bars_preserve_original_cell_colors_and_restore_theme(
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
            accent = app.get_css_variables()["accent"]
            for focused in (True, False):
                if focused:
                    editor.focus()
                else:
                    app.active_workspace.command_input.focus()
                strip = editor.render_line(0)
                cells = caret_cells(editor, 0)
                assert len(cells) == 3
                assert strip.cell_length == source.cell_length
                assert without_cells(strip, cells).text == source.text[: width - 3]
                for x in cells:
                    assert_caret_bar(strip, x, accent, bgcolor=original.bgcolor)
                expanded = text.expandtabs(editor.indent_width)
                plain = without_cells(strip, cells)
                for segment in plain.crop(editor.gutter_width, editor.gutter_width + len(expanded)):
                    assert segment.style.color == original.color
                    assert segment.style.bgcolor == original.bgcolor
                    assert not segment.style.underline
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
    background: str | None,
    monkeypatch: pytest.MonkeyPatch,
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
            accent = app.get_css_variables()["accent"]
            for focused in (True, False):
                if focused:
                    editor.focus()
                else:
                    app.active_workspace.command_input.focus()
                for row, column in ((0, 1), (1, 0)):
                    left = editor.gutter_width + column
                    assert_caret_bar(
                        editor.render_line(row), left, accent, bgcolor=original.bgcolor
                    )
            assert editor.text == "x\n"

    asyncio.run(exercise())


def test_mouse_targets_skip_bar_cells_and_clicking_a_bar_selects_its_caret() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "SELECT value\nFROM t"
            editor._set_selections(Selection.cursor((0, 3)), [Selection.cursor((0, 8))])
            await pilot.pause()
            # Native mapping treats every cell as a document column; bars do not.
            native = {
                column: TextArea.get_target_document_location(
                    editor, SimpleNamespace(x=column, y=0)
                )[1]
                for column in range(editor.gutter_width, editor.gutter_width + 14)
            }
            mapped = {
                column: editor.get_target_document_location(SimpleNamespace(x=column, y=0))[1]
                for column in native
            }
            bars = [3, 9]
            for column, native_column in native.items():
                if native_column < bars[0]:
                    assert mapped[column] == native_column
                elif native_column == bars[0]:
                    assert mapped[column] == 3
                elif native_column < bars[1]:
                    assert mapped[column] == native_column - 1
                elif native_column == bars[1]:
                    assert mapped[column] == 8
                else:
                    assert mapped[column] == min(native_column - 2, 12)
            # Rows not yet drawn fall back to the current caret layout.
            drawn = dict(editor._drawn_carets)
            editor._drawn_carets.clear()
            assert editor.get_target_document_location(
                SimpleNamespace(x=editor.gutter_width + 10, y=0)
            ) == (0, 8)
            editor._drawn_carets.update(drawn)
            row_one = SimpleNamespace(x=editor.gutter_width + 2, y=1)
            assert editor.get_target_document_location(
                row_one
            ) == TextArea.get_target_document_location(editor, row_one)
            # A real click on the displayed "u" lands on the "u", past both bars.
            offset_x = editor.content_region.x - editor.region.x + editor.gutter_width + 12
            offset_y = editor.content_region.y - editor.region.y
            await pilot.click("#query-editor", offset=(offset_x, offset_y))
            await pilot.pause()
            assert editor.cursor_location == (0, 10)
            assert editor.cursor_count == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("command_focus", [False, True])
@pytest.mark.parametrize("column", [0, 10])
def test_native_renderer_preserves_syntax_colors_around_bars(
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
            editor._set_selections(Selection.cursor((0, len(line))), [])
            await pilot.pause()
            baseline = [editor.render_line(row) for row in range(2)]
            editor._set_selections(Selection.cursor((0, column)), [Selection.cursor((1, column))])
            await pilot.pause()
            viewport = editor.scrollable_content_region
            accent = app.get_css_variables()["accent"]
            left = editor.gutter_width + column
            composed = app.screen._compositor.render_strips()
            for row in range(2):
                for strip in (
                    editor.render_line(row),
                    composed[viewport.y + row].crop(viewport.x, viewport.right),
                ):
                    assert strip.cell_length == viewport.width
                    assert_caret_bar(strip, left, accent)
                    plain = without_cells(strip, [left])
                    assert plain.crop(
                        editor.gutter_width, editor.gutter_width + len(line)
                    ).text == (line)
                    character = next(iter(plain.crop(left, left + 1))).style
                    original = next(iter(baseline[row].crop(left, left + 1))).style
                    assert character.color == original.color
                    assert not character.underline
                    if column == 0:
                        assert character.color == Style(color="#12ab34").color
            assert editor._theme is theme
            assert vars(theme) == theme_state
            assert editor.text == line + "\n" + line

    asyncio.run(exercise())


def test_end_caret_inside_stale_search_match_renders_dark_bar_before_refresh() -> None:
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
            caret = strip.crop(editor.gutter_width + 11, editor.gutter_width + 12)
            assert caret.text == CARET
            segment = next(iter(caret))
            assert segment.style is not None
            assert segment.style.color is not None
            assert segment.style.color.get_truecolor().hex == "#000000"
            assert segment.style.underline is False

            await pilot.pause()
            assert editor._search_matches == ()

    asyncio.run(exercise())


def test_main_caret_stays_visible_after_bars_shift_its_row() -> None:
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
            assert_caret_bar(strip, cells[-1], app.get_css_variables()["accent"])
            assert editor.virtual_size.width >= 300 + 4 + editor.gutter_width

    asyncio.run(exercise())


def test_mouse_mapping_ignores_carets_scrolled_out_on_the_left() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "x" * 300
            editor._set_selections(Selection.cursor((0, 150)), [Selection.cursor((0, 10))])
            await pilot.pause()
            scroll_x = int(editor.scroll_x)
            assert scroll_x > 10
            editor.render_line(0)
            column = scroll_x + 5
            event = SimpleNamespace(x=editor.gutter.left + editor.gutter_width + 5, y=0)
            assert editor.get_target_document_location(event) == (0, column)
            editor._drawn_carets.clear()
            assert editor.get_target_document_location(event) == (0, column)

    asyncio.run(exercise())


def test_soft_wrap_reserves_room_for_every_bar_on_a_full_row() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.soft_wrap = True
            editor.show_line_numbers = False
            await pilot.pause()
            text = "".join(chr(ord("a") + index % 26) for index in range(300))
            editor.text = text
            width = editor.scrollable_content_region.width
            carets = [Selection.cursor((0, column)) for column in (1, 2, 3)]
            editor._set_selections(Selection.cursor((0, width - 4)), carets)
            await pilot.pause()
            accent = app.get_css_variables()["accent"]
            shown = ""
            bars = 0
            for row in range(editor.wrapped_document.height):
                strip = editor.render_line(row - int(editor.scroll_y))
                cells = caret_cells(editor, row - int(editor.scroll_y))
                for x in cells:
                    assert x < width
                    assert_caret_bar(strip, x, accent)
                bars += len(cells)
                shown += without_cells(strip, cells).text.rstrip()
            assert bars == 4
            assert shown == text
            editor.collapse_to_active()
            await pilot.pause()
            assert editor._caret_reserve == 0

    asyncio.run(exercise())
