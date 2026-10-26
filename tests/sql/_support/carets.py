"""Expectations for SQL Explorer's VS Code-style caret bars."""

from __future__ import annotations

from typing import Any

from textual.strip import Strip

CARET = "▌"


def caret_cells(editor: Any, row: int) -> list[int]:
    """Return the widget-local x of each caret bar on a viewport row.

    Each bar takes its own cell before the character at its caret, so every bar
    further right is displaced by the bars inserted to its left.
    """
    visual_row = row + int(editor.scroll_y)
    offsets = sorted(
        x
        for x, y in (
            editor.wrapped_document.location_to_offset(selection.end)
            for selection in editor.cursor_selections
        )
        if y == visual_row
    )
    cells: list[int] = []
    for offset in offsets:
        x = offset - int(editor.scroll_x) + editor.gutter_width
        if x >= editor.gutter_width:
            cells.append(x + len(cells))
    return cells


def without_cells(strip: Strip, cells: list[int]) -> Strip:
    """Drop single cells from a rendered row, restoring the document layout."""
    for x in sorted(cells, reverse=True):
        strip = Strip.join([strip.crop(0, x), strip.crop(x + 1)])
    return strip


def assert_caret_bar(strip: Strip, x: int, accent: str, *, bgcolor: Any = "any") -> None:
    """Assert that cell ``x`` is a thick accent bar without text-masking attributes."""
    cell = list(strip.crop(x, x + 1))
    assert "".join(segment.text for segment in cell) == CARET
    for segment in cell:
        style = segment.style
        assert style is not None
        assert style.color is not None
        assert style.color.get_truecolor().hex == accent.lower()
        assert not style.bold
        assert not style.underline
        assert not style.reverse
        if bgcolor != "any":
            assert style.bgcolor == bgcolor


def assert_plain_cells(strip: Strip) -> None:
    """Assert that document characters are not underlined, reversed, or bolded by carets."""
    for segment in strip:
        style = segment.style
        if style is not None and segment.text.strip():
            assert not style.underline
            assert not style.reverse
