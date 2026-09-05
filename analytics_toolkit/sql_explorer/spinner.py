"""Portable three-row snake animation using terminal line characters."""

from __future__ import annotations

from rich.text import Text

# The horizontal strokes sit at the same height as the adjacent card borders.
_OUTLINE = ("┌───┐", "│   │", "└───┘")
_PATH = (
    (0, 0),
    (0, 1),
    (0, 2),
    (0, 3),
    (0, 4),
    (1, 4),
    (2, 4),
    (2, 3),
    (2, 2),
    (2, 1),
    (2, 0),
    (1, 0),
)
_TRAIL = ("#f2f4f5", "#c6cbd0", "#969ea6", "#69737d")


def _frame(index: int) -> Text:
    colors = {
        _PATH[(index - distance) % len(_PATH)]: color for distance, color in enumerate(_TRAIL)
    }
    rows = []
    for row, outline in enumerate(_OUTLINE):
        line = Text()
        for column, character in enumerate(outline):
            line.append(character, colors.get((row, column), "#454f59"))
        rows.append(line)
    return Text("\n").join(rows)


SNAKE_FRAMES = tuple(_frame(index) for index in range(len(_PATH)))
