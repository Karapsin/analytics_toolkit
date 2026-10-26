from __future__ import annotations

import asyncio

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from rich.segment import Segment
from rich.style import Style
from textual.document._document import Selection
from textual.strip import Strip
from textual.widgets import TextArea

from tests.sql._support.carets import assert_caret_block, caret_cells, input_cursor_style
from tests.sql._support.explorer import FakeSession


@pytest.mark.parametrize("background", [None, "#123456"])
def test_characters_beside_blocks_retain_original_foreground_and_background(
    monkeypatch: pytest.MonkeyPatch, background: str | None
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "SQL"
            editor._set_selections(Selection.cursor((0, 0)), [Selection.cursor((0, 2))])
            await pilot.pause()
            original = Style(color="#abcdef", bgcolor=background)
            source = Strip(
                [Segment(" " * editor.gutter_width), Segment("SQL", original), Segment(" " * 80)]
            )
            monkeypatch.setattr(TextArea, "render_line", lambda _self, _y: source)
            theme = editor._theme
            cursor = input_cursor_style(app)
            for focused in (True, False):
                if not focused:
                    app.active_workspace.command_input.focus()
                    await pilot.pause()
                rendered = editor.render_line(0)
                cells = caret_cells(editor, 0)
                assert cells == [editor.gutter_width, editor.gutter_width + 2]
                assert rendered.crop(editor.gutter_width, editor.gutter_width + 3).text == "SQL"
                assert_caret_block(rendered, cells[0], cursor, text="S")
                assert_caret_block(rendered, cells[1], cursor, text="L")
                middle = rendered.crop(editor.gutter_width + 1, editor.gutter_width + 2)
                assert middle.text == "Q"
                assert all(segment.style.color == original.color for segment in middle)
                assert all(segment.style.bgcolor == original.bgcolor for segment in middle)
                assert all(not segment.style.reverse for segment in middle)
                assert editor._theme is theme
            assert editor.text == "SQL"

    asyncio.run(exercise())
