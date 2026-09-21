"""Case-sensitive navigation sharing the editor's keyboard actions."""
# ruff: noqa: SLF001 -- commands and editor share cursor-state ownership.

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.document._document import Selection

from .editor_actions import column_coordinate, positive_count, row_coordinate

if TYPE_CHECKING:
    from .editor import SqlEditor

_COORDINATE_COUNT = 2
_SELECTION_COORDINATE_COUNT = 4
_MAX_NEW_LINES = 100
_MAX_NEW_COLUMNS = 10


def _destination(editor: SqlEditor, row: str, column: str) -> tuple[int, int]:
    target_row = row_coordinate(row, editor.document.line_count)
    return target_row, column_coordinate(column, editor.document[target_row])


def _inclusive_end(editor: SqlEditor, point: tuple[int, int]) -> tuple[int, int]:
    return point[0], min(point[1] + 1, len(editor.document[point[0]]))


def _relative(editor: SqlEditor, command: str, arguments: list[str]) -> None:
    if len(arguments) > 1:
        message = f"Usage: {command} [N]"
        raise ValueError(message)
    count = positive_count(arguments[0]) if arguments else 1
    step = max(1, editor.content_size.height) if command in {"pd", "pu"} else 1
    editor.move_rows(count * step * (-1 if command in {"u", "pu"} else 1))


def _absolute_selection(editor: SqlEditor, arguments: list[str]) -> None:
    if len(arguments) != _SELECTION_COORDINATE_COUNT:
        message = "Usage: s ROW COL ROW COL"
        raise ValueError(message)
    first = _destination(editor, *arguments[:_COORDINATE_COUNT])
    last = _destination(editor, *arguments[_COORDINATE_COUNT:])
    low, high = sorted((first, last))
    high = _inclusive_end(editor, high)
    editor._set_selections(Selection(low, high) if first <= last else Selection(high, low), [])


def _word_or_edge(editor: SqlEditor, command: str, arguments: list[str]) -> None:
    action = arguments[0].lower()
    if len(arguments) > (_COORDINATE_COUNT if action in {"n", "p"} else 1):
        message = f"Usage: {command} s|e|n [N]|p [N]"
        raise ValueError(message)
    count = positive_count(arguments[1]) if len(arguments) == _COORDINATE_COUNT else 1
    selecting = command == "mvs"
    if selecting:
        editor._set_selections(Selection.cursor(editor.selection.end), [])
    handler = {
        "s": editor.action_cursor_line_start,
        "e": editor.action_cursor_line_end,
        "n": editor.action_cursor_word_right,
        "p": editor.action_cursor_word_left,
    }[action]
    for _ in range(min(count, len(editor.text) + 1)):
        handler(selecting)


def _absolute_move(editor: SqlEditor, command: str, arguments: list[str]) -> None:
    selecting = command == "mvs"
    if (selecting and len(arguments) != _COORDINATE_COUNT) or len(arguments) > _COORDINATE_COUNT:
        message = f"Usage: {command} ROW COL"
        raise ValueError(message)
    coordinates = arguments + ["S"] * (_COORDINATE_COUNT - len(arguments))
    if not selecting:
        coordinates = _extend_move_destination(editor, coordinates)
    target = _destination(editor, *coordinates)
    anchor = editor.selection.end
    if selecting:
        end = _inclusive_end(editor, target) if target >= anchor else target
        editor._set_selections(Selection(anchor, end), [])
    else:
        editor._set_selections(Selection.cursor(target), [])


def _extend_move_destination(editor: SqlEditor, coordinates: list[str]) -> list[str]:
    row, column = coordinates
    line_count = editor.document.line_count
    destination = (
        row_coordinate(row, line_count) + 1
        if row in {"S", "E"}
        else min(int(row), line_count + _MAX_NEW_LINES)
    )
    target_row = row_coordinate(str(destination), max(line_count, destination))
    line = editor.document[target_row] if target_row < line_count else ""
    target_column = column_coordinate(column, line)
    if column not in {"S", "E"}:
        target_column = min(int(column) - 1, len(line) + _MAX_NEW_COLUMNS)
    extra_lines = max(0, destination - line_count)
    insertion = "\n" * extra_lines + " " * max(0, target_column - len(line))
    if insertion:
        last = editor.document.line_count - 1
        end = (last, len(editor.document[last])) if extra_lines else (target_row, len(line))
        editor.history.checkpoint()
        editor.replace(insertion, end, end, maintain_selection_offset=False)
        editor.history.checkpoint()
    return [str(destination), str(target_column + 1)]


def move_command(editor: SqlEditor, command: str, arguments: list[str]) -> None:
    """Move or select, allowing only absolute mv to extend the document."""
    if command in {"d", "u", "pd", "pu"}:
        _relative(editor, command, arguments)
    elif command == "cursor":
        if len(arguments) != _COORDINATE_COUNT or arguments[1] not in {"u", "d"}:
            message = "Usage: cursor N u|d"
            raise ValueError(message)
        editor.add_cursors(positive_count(arguments[0]), -1 if arguments[1] == "u" else 1)
    elif command == "s":
        _absolute_selection(editor, arguments)
    elif command in {"start", "end"}:
        if arguments:
            message = f"Usage: {command}"
            raise ValueError(message)
        _absolute_move(editor, "mv", ["S", "S"] if command == "start" else ["E", "E"])
    elif (arguments and arguments[0] in {"s", "e", "n", "p"}) or (
        command == "mvs" and arguments in (["S"], ["E"])
    ):
        _word_or_edge(editor, command, arguments)
    else:
        _absolute_move(editor, command, arguments)
