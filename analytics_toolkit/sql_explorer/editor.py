"""SQL editor widget with multi-cursor editing support."""
# ruff: noqa: FBT001, FBT002

from __future__ import annotations

import re
from time import monotonic
from typing import TYPE_CHECKING, Any, ClassVar, Tuple, cast

from rich.style import Style
from textual.binding import Binding, BindingType
from textual.document._document import Selection
from textual.widgets import TextArea
from typing_extensions import TypeAlias

if TYPE_CHECKING:
    from rich.text import Text
    from textual import events

    from .app import SqlExplorerApp


_INDENT = "    "
_DOUBLE_CLICK_SECONDS = 0.5
_SEARCH_MATCH_STYLE = Style(color="black", bgcolor="bright_yellow", bold=True)
SearchMatch: TypeAlias = Tuple[Tuple[int, int], Tuple[int, int]]


class SqlEditor(TextArea):
    """A Textual editor with independent selections on separate logical lines."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "cursor_up", "Cursor up", show=False),
        Binding("down", "cursor_down", "Cursor down", show=False),
        Binding("home", "home", "Line start", show=False),
        Binding("end", "cursor_line_end", "Line end", show=False),
        Binding("shift+up", "add_cursor_above", "Add cursor above", show=False),
        Binding("shift+down", "add_cursor_below", "Add cursor below", show=False),
        Binding("shift+home", "cursor_line_start(True)", "Select to line start", show=False),
        Binding("shift+end", "cursor_line_end(True)", "Select to line end", show=False),
        Binding("ctrl+a", "select_all", "Select all", show=False),
        Binding("ctrl+x", "cut", "Cut", show=False),
        Binding("ctrl+v", "paste", "Paste", show=False),
        Binding("ctrl+z", "undo", "Undo", show=False),
        Binding("ctrl+y,ctrl+shift+z", "redo", "Redo", show=False),
        Binding("ctrl+home", "cursor_document_start", "Document start", show=False),
        Binding("ctrl+end", "cursor_document_end", "Document end", show=False),
        Binding("tab", "completion_or_indent", "Complete or indent", show=False),
        Binding("shift+tab", "unindent", "Unindent", show=False),
    ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._secondary_selections: list[Selection] = []
        self._search_pattern = ""
        self._search_matches: tuple[SearchMatch, ...] = ()
        self._search_index = -1
        self._last_click_at = 0.0
        self._last_click_location: tuple[int, int] | None = None
        kwargs.setdefault("language", "sql")
        kwargs.setdefault("tab_behavior", "indent")
        try:
            super().__init__(*args, **kwargs)
        except (ImportError, ModuleNotFoundError, ValueError):
            kwargs["language"] = None
            super().__init__(*args, **kwargs)
        self.cursor_blink = False
        if not self.soft_wrap:
            self._refresh_size()

    @property
    def cursor_count(self) -> int:
        return len(self._secondary_selections) + 1

    @property
    def cursor_selections(self) -> tuple[Selection, ...]:
        """Return all selections in document order, with the active one included."""
        return tuple(sorted((*self._secondary_selections, self.selection), key=self._selection_key))

    @property
    def cursor_render_offset(self) -> tuple[int, int]:
        return self._cursor_offset[0], self._cursor_offset[1]

    @property
    def search_match_count(self) -> int:
        return len(self._search_matches)

    @property
    def search_position(self) -> int | None:
        return self._search_index + 1 if self._search_index >= 0 else None

    @staticmethod
    def _selection_key(selection: Selection) -> tuple[tuple[int, int], tuple[int, int]]:
        return tuple(sorted(selection))  # type: ignore[return-value]

    def _set_selections(self, active: Selection, secondary: list[Selection]) -> None:
        seen_rows = {active.end[0]}
        retained: list[Selection] = []
        for selection in secondary:
            if selection.end[0] not in seen_rows:
                retained.append(selection)
                seen_rows.add(selection.end[0])
        self._secondary_selections = retained
        self.selection = active
        self.refresh()

    def collapse_to_active(self) -> None:
        if self._secondary_selections:
            self._secondary_selections = []
            self.refresh()

    def _collapse_for_single_cursor_action(self) -> None:
        self.collapse_to_active()

    def action_add_cursor_above(self) -> None:
        self._add_cursor(-1)

    def action_add_cursor_below(self) -> None:
        self._add_cursor(1)

    def _add_cursor(self, direction: int) -> None:
        active = self.selection
        row, column = active.end
        target_row = row + direction
        if target_row < 0 or target_row >= self.document.line_count:
            return
        existing = next(
            (
                selection
                for selection in self._secondary_selections
                if selection.end[0] == target_row
            ),
            None,
        )
        if existing is not None:
            self._secondary_selections.remove(existing)
            self.refresh()
            return
        target = Selection.cursor((target_row, min(column, len(self.document[target_row]))))
        self._set_selections(target, [*self._secondary_selections, active])

    def move_to_line_start(self, line_number: int) -> None:
        self._set_selections(Selection.cursor((line_number - 1, 0)), [])
        self.focus()

    def select_to_line_start(self, line_number: int) -> None:
        self._set_selections(Selection(self.selection.end, (line_number - 1, 0)), [])
        self.focus()

    def command_copy_text(self) -> str:
        selected = [selection for selection in self.cursor_selections if not selection.is_empty]
        if not selected:
            return self.text
        return "\n".join(self.document.get_text_range(*sorted(selection)) for selection in selected)

    def paste_clipboard(self, value: str) -> bool:
        if not value:
            return False
        self._apply_batch_edit(value)
        self.focus()
        return True

    def _apply_batch_edit(
        self,
        insert: str,
        *,
        delete_left: bool = False,
        delete_right: bool = False,
    ) -> None:
        entries = [(False, selection) for selection in self._secondary_selections]
        entries.append((True, self.selection))
        results: list[tuple[bool, Selection]] = []
        self.history.checkpoint()
        for active, selection in sorted(
            entries, key=lambda item: self._selection_key(item[1]), reverse=True
        ):
            start, end = sorted(selection)
            if selection.is_empty and delete_left:
                start = self._left_of(end)
            elif selection.is_empty and delete_right:
                end = self._right_of(start)
            result = self.replace(insert, start, end, maintain_selection_offset=False)
            results.append((active, Selection.cursor(result.end_location)))
        self.history.checkpoint()
        active_selection = next(selection for active, selection in results if active)
        self._set_selections(
            active_selection, [selection for active, selection in results if not active]
        )

    def _move_all(self, location_for: Any, *, select: bool = False) -> None:
        entries = [(False, selection) for selection in self._secondary_selections]
        entries.append((True, self.selection))
        moved: list[tuple[bool, Selection]] = []
        for active, selection in entries:
            target = location_for(selection.end)
            moved.append(
                (active, Selection(selection.start, target) if select else Selection.cursor(target))
            )
        active_selection = next(selection for active, selection in moved if active)
        self._set_selections(
            active_selection, [selection for active, selection in moved if not active]
        )

    def action_cursor_up(self, select: bool = False) -> None:
        if self.cursor_count == 1 and not select and self.cursor_location[0] == 0:
            cast("SqlExplorerApp", self.app).action_focus_previous_pane()
            return
        self._move_all(
            lambda location: (
                max(0, location[0] - 1),
                min(location[1], len(self.document[max(0, location[0] - 1)])),
            ),
            select=select,
        )

    def action_cursor_down(self, select: bool = False) -> None:
        last_row = self.document.line_count - 1
        if self.cursor_count == 1 and not select and self.cursor_location[0] == last_row:
            cast("SqlExplorerApp", self.app).action_focus_next_pane()
            return
        self._move_all(
            lambda location: (
                min(last_row, location[0] + 1),
                min(location[1], len(self.document[min(last_row, location[0] + 1)])),
            ),
            select=select,
        )

    def _left_of(self, location: tuple[int, int]) -> tuple[int, int]:
        row, column = location
        return (
            (row, column - 1) if column else (max(0, row - 1), len(self.document[max(0, row - 1)]))
        )

    def _right_of(self, location: tuple[int, int]) -> tuple[int, int]:
        row, column = location
        if column < len(self.document[row]):
            return row, column + 1
        return (row + 1, 0) if row < self.document.line_count - 1 else location

    def action_cursor_left(self, select: bool = False) -> None:
        self._move_all(self._left_of, select=select)

    def action_cursor_right(self, select: bool = False) -> None:
        self._move_all(self._right_of, select=select)

    def action_home(self) -> None:
        self._move_all(lambda location: (location[0], 0))

    def action_cursor_line_start(self, select: bool = False) -> None:
        self._move_all(lambda location: (location[0], 0), select=select)

    def action_cursor_line_end(self, select: bool = False) -> None:
        self._move_all(
            lambda location: (location[0], len(self.document[location[0]])), select=select
        )

    def action_cursor_document_start(self) -> None:
        self._move_all(lambda _location: (0, 0))

    def action_cursor_document_end(self) -> None:
        last_row = self.document.line_count - 1
        self._move_all(lambda _location: (last_row, len(self.document[last_row])))

    def action_select_all(self) -> None:
        self._collapse_for_single_cursor_action()
        super().action_select_all()

    def action_delete_left(self) -> None:
        self._apply_batch_edit("", delete_left=True)

    def action_delete_right(self) -> None:
        self._apply_batch_edit("", delete_right=True)

    def action_cut(self) -> None:
        value = self.command_copy_text()
        if any(not selection.is_empty for selection in self.cursor_selections):
            cast("SqlExplorerApp", self.app).copy_to_explorer_clipboard(value)
            self._apply_batch_edit("")

    def action_paste(self) -> None:
        self.paste_clipboard(cast("SqlExplorerApp", self.app).paste_from_explorer_clipboard())

    async def _on_paste(self, event: events.Paste) -> None:
        if not self.read_only:
            self._apply_batch_edit(event.text)

    async def _on_key(self, event: events.Key) -> None:
        application = cast("SqlExplorerApp", self.app)
        from .widgets import CompletionMenu  # noqa: PLC0415 -- avoids widget import cycle.

        menu = application.query_one(CompletionMenu)
        if menu.is_open and event.key in {"up", "down", "enter", "escape"}:
            event.stop()
            event.prevent_default()
            if event.key == "enter":
                application.action_plain_tab()
            elif event.key == "escape":
                menu.action_close()
            else:
                menu.move_highlight(-1 if event.key == "up" else 1)
            return
        if event.key == "tab":
            event.stop()
            event.prevent_default()
            application.action_plain_tab()
            return
        if event.key == "escape" and self.cursor_count > 1:
            event.stop()
            event.prevent_default()
            application.action_escape()
            return
        if not self.read_only and (event.is_printable or event.key == "enter"):
            event.stop()
            event.prevent_default()
            self._apply_batch_edit("\n" if event.key == "enter" else (event.character or ""))
            return
        await super()._on_key(event)

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        self._collapse_for_single_cursor_action()
        await super()._on_mouse_down(event)

    async def _on_click(self, event: events.Click) -> None:
        location = self.get_target_document_location(event)
        clicked_at = monotonic()
        previous = self._last_click_location
        is_double = (
            previous is not None
            and previous[0] == location[0]
            and abs(previous[1] - location[1]) <= 1
            and clicked_at - self._last_click_at <= _DOUBLE_CLICK_SECONDS
        )
        self._last_click_at = clicked_at
        self._last_click_location = location
        if is_double:
            self.action_double_click_word(location)
            self._last_click_at = 0.0
            self._last_click_location = None
            event.stop()

    def action_double_click_word(self, location: tuple[int, int]) -> None:
        self._collapse_for_single_cursor_action()
        row, column = location
        line = self.document[row]
        if not line:
            self.move_cursor((row, column))
            return
        column = min(column, len(line) - 1)
        if not (line[column].isalnum() or line[column] == "_"):
            self.move_cursor((row, column))
            return
        start = column
        end = column + 1
        while start and (line[start - 1].isalnum() or line[start - 1] == "_"):
            start -= 1
        while end < len(line) and (line[end].isalnum() or line[end] == "_"):
            end += 1
        self.move_cursor((row, start))
        self.move_cursor((row, end), select=True)

    def set_search_pattern(self, pattern: str) -> int:
        self._search_pattern = pattern
        self.refresh_search_matches()
        return self.search_match_count

    def refresh_search_matches(self) -> None:
        matches: list[SearchMatch] = []
        if self._search_pattern:
            expression = re.compile(re.escape(self._search_pattern), flags=re.IGNORECASE)
            for row in range(self.document.line_count):
                matches.extend(
                    ((row, match.start()), (row, match.end()))
                    for match in expression.finditer(self.document[row])
                )
        self._search_matches = tuple(matches)
        self._search_index = next(
            (
                index
                for index, match in enumerate(matches)
                if match == tuple(sorted(self.selection))
            ),
            -1,
        )
        self.refresh()

    def clear_search(self) -> None:
        self._search_pattern = ""
        self.refresh_search_matches()

    def _select_search_match(self, index: int) -> None:
        self._collapse_for_single_cursor_action()
        start, end = self._search_matches[index]
        self.move_cursor(start)
        self.move_cursor(end, select=True, center=True)

    def select_next_search_match(self) -> int | None:
        if not self._search_matches:
            return None
        self._search_index = (self._search_index + 1) % len(self._search_matches)
        self._select_search_match(self._search_index)
        return self._search_index + 1

    def replace_current_search_match(self, replacement: str) -> bool:
        self._collapse_for_single_cursor_action()
        if not self._search_matches:
            return False
        start, end = self._search_matches[max(self._search_index, 0)]
        result = self.replace(replacement, start, end, maintain_selection_offset=False)
        resume_at = result.end_location
        self.refresh_search_matches()
        if self._search_matches:
            self._search_index = next(
                (
                    index
                    for index, (match_start, _) in enumerate(self._search_matches)
                    if match_start >= resume_at
                ),
                0,
            )
            self._select_search_match(self._search_index)
        return True

    def replace_all_search_matches(self, replacement: str) -> int:
        self._collapse_for_single_cursor_action()
        if not self._search_pattern:
            return 0
        updated, count = re.subn(
            re.escape(self._search_pattern), lambda _: replacement, self.text, flags=re.IGNORECASE
        )
        if count:
            self.text = updated
        self.refresh_search_matches()
        return count

    def get_line(self, line_index: int) -> Text:
        line = super().get_line(line_index)
        for (start_row, start_column), (end_row, end_column) in self._search_matches:
            if start_row == line_index == end_row:
                line.stylize(_SEARCH_MATCH_STYLE, start_column, end_column)
        theme = self._theme
        for selection in self._secondary_selections:
            start, end = sorted(selection)
            if (
                start[0] <= line_index <= end[0]
                and start != end
                and theme
                and theme.selection_style
            ):
                line.stylize(
                    theme.selection_style,
                    start[1] if line_index == start[0] else 0,
                    end[1] if line_index == end[0] else len(line),
                )
            if selection.end[0] == line_index and theme and theme.cursor_style:
                column = selection.end[1]
                line.stylize(theme.cursor_style, max(0, column - 1), max(1, column))
        return line

    def action_completion_or_indent(self) -> None:
        if self.cursor_count > 1:
            self._apply_batch_edit(_INDENT)
            return
        cast("SqlExplorerApp", self.app).action_plain_tab()

    def action_indent(self) -> None:
        if self.cursor_count > 1:
            self._apply_batch_edit(_INDENT)
            return
        start, end = self.selection
        if start == end:
            result = self.replace(_INDENT, start, end)
            self.cursor_location = result.end_location
            return
        low, high = sorted((start, end))
        first = low[0]
        last = high[0] - (high[1] == 0 and high[0] > low[0])
        lines = self.text.splitlines(keepends=True)
        for row in range(last, first - 1, -1):
            lines[row] = _INDENT + lines[row]
        self.text = "".join(lines)
        self.selection = Selection(
            (low[0], 0), (high[0], high[1] + (len(_INDENT) if high[0] <= last else 0))
        )

    def action_unindent(self) -> None:
        self._collapse_for_single_cursor_action()
        start, end = self.selection
        if start != end:
            low, high = sorted((start, end))
            first = low[0]
            last = high[0] - (high[1] == 0 and high[0] > low[0])
            lines = self.text.splitlines(keepends=True)
            removed: dict[int, int] = {}
            for row in range(first, last + 1):
                amount = min(len(lines[row]) - len(lines[row].lstrip(" ")), 4)
                removed[row] = amount
                lines[row] = lines[row][amount:]
            self.text = "".join(lines)
            self.selection = Selection(
                (low[0], max(0, low[1] - removed.get(low[0], 0))),
                (high[0], max(0, high[1] - removed.get(high[0], 0))),
            )
            return
        row, column = self.cursor_location
        line = self.document[row]
        removable = min(len(line) - len(line.lstrip(" ")), 4, column)
        if removable:
            self.replace("", (row, 0), (row, removable))
            self.cursor_location = (row, column - removable)
