from __future__ import annotations

import asyncio

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from rich.segment import Segment
from rich.style import Style
from textual.document._document import Selection
from textual.strip import Strip
from textual.widgets import TextArea

from tests.sql._support.explorer import FakeSession


@pytest.mark.parametrize("background", [None, "#123456"])
def test_occupied_caret_retains_original_foreground_and_background(
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
            for focused in (True, False):
                if not focused:
                    app.active_workspace.command_input.focus()
                    await pilot.pause()
                rendered = editor.render_line(0)
                assert rendered.crop(editor.gutter_width, editor.gutter_width + 3).text == "SQL"
                for column in (0, 2):
                    cell = rendered.crop(
                        editor.gutter_width + column, editor.gutter_width + column + 1
                    )
                    assert all(segment.style.color == original.color for segment in cell)
                    assert all(segment.style.bgcolor == original.bgcolor for segment in cell)
                    assert all(segment.style.underline for segment in cell)
                    assert all(segment.style.bold is False for segment in cell)
                assert editor._theme is theme
            assert editor.text == "SQL"

    asyncio.run(exercise())
