"""A column ruler aligned with the main cursor's logical line."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text
from textual.widgets import Static

if TYPE_CHECKING:
    from .editor import SqlEditor

_LABEL_INTERVAL = 10


def column_numbers(
    line: str, offset: int, width: int, tab_width: int, column_count: int | None = None
) -> dict[int, int]:
    """Map visible cells through the shared document column range."""
    numbers: dict[int, int] = {}
    cell = 0
    column = 0
    count = len(line) + 1 if column_count is None else column_count
    while cell < offset + width and column < count:
        character = line[column] if column < len(line) else " "
        advance = tab_width - cell % tab_width if character == "\t" else cell_len(character)
        if advance and cell >= offset:
            numbers[cell - offset] = column + 1
        cell += advance
        column += 1
    return numbers


def ruler_labels(numbers: dict[int, int], width: int, active: int, accent: str) -> Text:
    """Render sparse, readable labels with the active column taking precedence."""
    text = [" "] * width
    active_cell = next((cell for cell, number in numbers.items() if number == active), None)
    label = str(active)
    active_start = max(0, active_cell - len(label) + 1) if active_cell is not None else width
    active_end = min(width, active_start + len(label))
    for cell, number in numbers.items():
        if number != 1 and number % _LABEL_INTERVAL:
            continue
        value = str(number)
        start = cell - len(value) + 1
        if start < 0 or (
            active_cell is not None and start <= active_end and cell >= active_start - 1
        ):
            continue
        text[start : cell + 1] = value
    text[active_start:active_end] = label[: active_end - active_start]
    result = Text("".join(text), no_wrap=True, overflow="crop")
    if active_start < active_end:
        result.stylize(f"bold {accent}", active_start, active_end)
    return result


class ColumnRuler(Static):
    def __init__(self, editor: SqlEditor) -> None:
        super().__init__(id="column-ruler")
        self.editor = editor

    def on_mount(self) -> None:
        self.watch(self.editor, "scroll_x", self.refresh_numbers)
        self.watch(self.editor, "selection", self.refresh_numbers)

    def refresh_numbers(self) -> None:
        self.refresh(layout=True)

    def render(self) -> Text:
        editor = self.editor
        row, column = editor.cursor_location
        gutter = editor.gutter_width + editor.gutter.left
        width = max(
            0,
            self.size.width - gutter - editor.gutter.right - int(editor.show_vertical_scrollbar),
        )
        numbers = column_numbers(
            editor.document[row],
            int(editor.scroll_x),
            width,
            editor.indent_width,
            max(map(len, editor.document.lines)) + 1,
        )
        active_cell = editor.cursor_render_offset[0] - int(editor.scroll_x)
        if 0 <= active_cell < width:
            numbers[active_cell] = column + 1
        accent = self.app.get_css_variables()["accent"]
        result = Text(" " * gutter, no_wrap=True, overflow="crop")
        result.append_text(ruler_labels(numbers, width, column + 1, accent))
        return result
