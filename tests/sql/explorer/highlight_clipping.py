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

from tests.sql._support.carets import assert_caret_block, caret_cells, input_cursor_style
from tests.sql._support.explorer import FakeSession


@pytest.mark.parametrize("scroll_x", [0, 8])
def test_multiline_highlight_clips_both_ends_and_preserves_occupied_carets(
    monkeypatch: pytest.MonkeyPatch, scroll_x: int
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            lines = ["SELECT " + "x" * 180] * 80
            editor.text = "\n".join(lines)
            editor._set_selections(Selection.cursor((2, 10)), [Selection.cursor((3, 12))])
            await pilot.pause()
            editor.scroll_to(x=scroll_x, y=1, animate=False, force=True)
            await pilot.pause()
            assert int(editor.scroll_x) == scroll_x
            assert int(editor.scroll_y) == 1
            # Supply a multiline match at the renderer boundary: ordinary find
            # currently produces single-line matches.
            editor._search_matches = (((1, 4), (3, 16)),)
            viewport = editor.scrollable_content_region
            width = viewport.width
            gutter = editor.gutter_width
            source_style = Style(color="#abcdef", bgcolor="#123456")

            def native_line(_editor: TextArea, y: int) -> Strip:
                text = lines[y + int(editor.scroll_y)][scroll_x : scroll_x + width - gutter]
                strip = Strip([Segment(" " * gutter), Segment(text, source_style)])
                return strip.adjust_cell_length(width)

            monkeypatch.setattr(TextArea, "render_line", native_line)
            for row in range(4):
                strip = editor.render_line(row)
                cells = caret_cells(editor, row)
                assert strip.text == native_line(editor, row).text
                assert strip.cell_length == width
                for x in range(gutter, width):
                    if x in cells:
                        continue
                    column = x - gutter + scroll_x
                    expected = row < 3 and (row != 0 or column >= 4)
                    expected = expected and (row != 2 or column < 16)
                    cell = strip.crop(x, x + 1)
                    assert all(
                        (segment.style.bgcolor.get_truecolor().hex == "#ffff00") is expected
                        for segment in cell
                    )
            for row, column in ((2, 10), (3, 12)):
                x = gutter + column - scroll_x
                rendered = editor.render_line(row - 1)
                assert caret_cells(editor, row - 1) == [x]
                # Inside the find match, the caret is the same input-cursor block.
                assert_caret_block(rendered, x, input_cursor_style(app), text=lines[row][column])
                following = rendered.crop(x + 1, x + 2)
                assert following.text == lines[row][column + 1]
                assert all(
                    segment.style.bgcolor.get_truecolor().hex == "#ffff00" for segment in following
                )
            assert editor.text == "\n".join(lines)

    asyncio.run(exercise())


@pytest.mark.parametrize("missing_theme", [False, True])
def test_unfocused_secondary_selection_uses_component_style_when_theme_style_absent(
    missing_theme: bool,
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "SELECT value\nFROM orders"
            editor._set_selections(Selection((0, 0), (0, 6)), [Selection((1, 0), (1, 4))])
            app.active_workspace.command_input.focus()
            await pilot.pause()
            original = editor._theme
            try:
                editor._theme = None if missing_theme else copy(original)
                if editor._theme is not None:
                    editor._theme.selection_style = None
                expected = editor.get_component_rich_style("text-area--selection")
                assert editor._selection_style() == expected
                for row, end in ((0, 6), (1, 4)):
                    line = editor.get_line(row)
                    assert line.plain == editor.document[row]
                    assert any(
                        span.start == 0 and span.end == end and span.style == expected
                        for span in line.spans
                    )
                assert editor.text == "SELECT value\nFROM orders"
            finally:
                editor._theme = original

    asyncio.run(exercise())
