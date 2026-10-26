"""Expectations for SQL Explorer's block carets, styled like the command pane input cursor."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rich.style import Style
    from textual.strip import Strip


def input_cursor_style(app: Any) -> Style:
    """Return the command pane input's own cursor style, the reference for editor carets."""
    command = app.active_workspace.command_input
    return command.get_component_rich_style("input--cursor")


def caret_cells(editor: Any, row: int) -> list[int]:
    """Return the widget-local x of each visible caret cell on a viewport row.

    Block carets are drawn on the character cell at the caret, so no caret
    displaces another one or the document text.
    """
    visual_row = row + int(editor.scroll_y)
    cells = sorted(
        x - int(editor.scroll_x) + editor.gutter_width
        for x, y in (
            editor.wrapped_document.location_to_offset(selection.end)
            for selection in editor.cursor_selections
        )
        if y == visual_row
    )
    return [x for x in cells if x >= editor.gutter_width]


def is_caret_cell(style: Style | None, cursor: Style) -> bool:
    """Return whether a rendered cell style carries the input cursor colors."""
    return (
        style is not None
        and style.color == cursor.color
        and style.bgcolor == cursor.bgcolor
        and bool(style.reverse) == bool(cursor.reverse)
    )


def assert_caret_block(
    strip: Strip, x: int, cursor: Style, *, text: str | None = None, width: int = 1
) -> None:
    """Assert that ``width`` cells at ``x`` are a block caret in the input cursor colors."""
    cells = strip.crop(x, x + width)
    assert cells.cell_length == width
    if text is not None:
        assert cells.text == text
    for segment in cells:
        if segment.cell_length:
            assert is_caret_cell(segment.style, cursor), segment


def assert_no_caret_cells(strip: Strip, cursor: Style, cells: list[int] | None = None) -> None:
    """Assert that no single cell outside ``cells`` uses the caret colors."""
    allowed = set(cells or ())
    for x in range(strip.cell_length):
        if x in allowed:
            continue
        for segment in strip.crop(x, x + 1):
            if segment.cell_length:
                assert not is_caret_cell(segment.style, cursor), (x, segment)
