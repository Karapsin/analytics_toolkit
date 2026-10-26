from __future__ import annotations

import asyncio

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.editor import SqlEditor
from rich.segment import Segment
from rich.style import Style
from textual.document._document import Selection
from textual.strip import Strip
from textual.widgets import TextArea

from tests.sql._support.carets import CARET, assert_caret_bar, caret_cells, without_cells
from tests.sql._support.explorer import FakeSession


def test_idle_before_mount_preserves_editor_without_accessing_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = SqlEditor("SELECT 1", soft_wrap=False)
            assert not editor.is_mounted
            original = editor.text, editor.cursor_selections

            def unexpected(_editor: SqlEditor) -> None:
                pytest.fail("Unmounted idle must not synchronize scrollbars or access the app")

            with monkeypatch.context() as patch:
                patch.setattr(SqlEditor, "_sync_horizontal_scrollbar", unexpected)
                editor.on_idle()
            assert (editor.text, editor.cursor_selections) == original
            assert not editor.is_mounted

    asyncio.run(exercise())


def test_theme_absence_preserves_cells_carets_and_restores_state_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "SELECT value"
            editor._set_selections(Selection.cursor((0, 0)), [Selection.cursor((0, 7))])
            await pilot.pause()
            width = editor.scrollable_content_region.width
            source = Strip(
                [Segment(" " * editor.gutter_width), Segment(editor.text, Style(color="#abcdef"))]
            ).adjust_cell_length(width)

            def native_line(_editor: TextArea, _row: int) -> Strip:
                assert editor._theme is None
                return source

            native_render = TextArea.render_line
            monkeypatch.setattr(TextArea, "render_line", native_line)
            theme = editor._theme
            try:
                editor._theme = None
                rendered = editor.render_line(0)
                cells = caret_cells(editor, 0)
                assert cells == [editor.gutter_width, editor.gutter_width + 8]
                assert without_cells(rendered, cells).text == source.text[: width - 2]
                assert rendered.cell_length == width
                accent = app.get_css_variables()["accent"]
                for x in cells:
                    assert_caret_bar(rendered, x, accent)
                assert editor._theme is None

                def fail(_editor: TextArea, _row: int) -> Strip:
                    message = "native renderer failed"
                    raise RuntimeError(message)

                monkeypatch.setattr(TextArea, "render_line", fail)
                with pytest.raises(RuntimeError, match="native renderer failed"):
                    editor.render_line(0)
                assert editor._theme is None
                assert editor.text == "SELECT value"
            finally:
                editor._theme = theme
                monkeypatch.setattr(TextArea, "render_line", native_render)
            assert editor._theme is theme

    asyncio.run(exercise())


@pytest.mark.parametrize("side", ["left", "right"])
def test_wholly_clipped_search_match_leaves_visible_match_and_cell_width_intact(
    monkeypatch: pytest.MonkeyPatch, side: str
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            text = "match " + "x" * 100 + " match " + "x" * 100 + " match"
            editor.text = text
            editor._set_selections(Selection.cursor((0, 115)), [])
            await pilot.pause()
            editor.scroll_to(x=100, animate=False, force=True)
            await pilot.pause()
            assert int(editor.scroll_x) == 100
            width = editor.scrollable_content_region.width
            gutter = editor.gutter_width
            visible = text[100 : 100 + width - gutter]
            source = Strip(
                [Segment(" " * gutter), Segment(visible, Style(color="#abcdef", bgcolor="#123456"))]
            ).adjust_cell_length(width)
            monkeypatch.setattr(TextArea, "render_line", lambda _editor, _row: source)
            hidden = ((0, 0), (0, 5)) if side == "left" else ((0, 214), (0, 219))
            editor._search_matches = (hidden, ((0, 107), (0, 112)))
            rendered = editor.render_line(0)
            assert caret_cells(editor, 0) == [gutter + 15]
            assert rendered.crop(gutter + 15, gutter + 16).text == CARET
            assert rendered.cell_length == width
            rendered = without_cells(rendered, [gutter + 15])
            assert rendered.text == source.text[: width - 1]
            for x in range(gutter, gutter + len(visible) - 1):
                cell = rendered.crop(x, x + 1)
                highlighted = 107 <= x - gutter + 100 < 112
                assert all(
                    (segment.style.bgcolor.get_truecolor().hex == "#ffff00") is highlighted
                    for segment in cell
                )
            assert editor.text == text

    asyncio.run(exercise())
