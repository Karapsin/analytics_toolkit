"""SQL editor widget with multi-cursor editing support."""
# ruff: noqa: FBT001, FBT002

from __future__ import annotations

import re
from copy import copy
from time import monotonic
from typing import TYPE_CHECKING, Any, ClassVar, Tuple, cast

from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.binding import Binding, BindingType
from textual.document._document import Document, Selection
from textual.strip import Strip
from textual.widgets import Input, TextArea
from typing_extensions import TypeAlias

from .clipboard import paste_fragments
from .editor_actions import code_context, cursor_edit

if TYPE_CHECKING:
    from rich.text import Text

    from .app import SqlExplorerApp


_INDENT = "    "
_DOUBLE_CLICK_SECONDS = 0.5
_SEARCH_MATCH_STYLE = Style(color="#000000", bgcolor="#ffff00", bold=True)
CARET_COMPONENT = "sql-editor--caret"
# Editor carets reuse the command pane input's block cursor rule and theme variables.
_INPUT_CURSOR_CSS = (
    """
    SqlEditor > .sql-editor--caret {
        background: $input-cursor-background;
        color: $input-cursor-foreground;
        text-style: $input-cursor-text-style;
    }
    SqlEditor:ansi > .sql-editor--caret {
        text-style: reverse;
    }
    """
    if "$input-cursor-background" in Input.DEFAULT_CSS
    else """
    SqlEditor > .sql-editor--caret {
        background: $surface;
        color: $text;
        text-style: reverse;
    }
    """
)
SearchMatch: TypeAlias = Tuple[Tuple[int, int], Tuple[int, int]]


class _CaretRenderTheme:
    """Resolve native styles on a copy, then leave caret styling to the overlay."""

    def __init__(self, theme: Any) -> None:
        self._resolved = copy(theme)

    def apply_css(self, text_area: TextArea) -> None:
        self._resolved.apply_css(text_area)
        self._resolved.cursor_style = Style()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolved, name)


class SqlEditor(TextArea):
    """A Textual editor with independent cursors and anchored selections."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {*TextArea.COMPONENT_CLASSES, CARET_COMPONENT}
    DEFAULT_CSS = _INPUT_CURSOR_CSS

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "cursor_up", "Cursor up", show=False),
        Binding("down", "cursor_down", "Cursor down", show=False),
        Binding("home", "cursor_line_start", "Line start", show=False),
        Binding("end", "cursor_line_end", "Line end", show=False),
        Binding("ctrl+left", "cursor_word_left", "Cursor word left", show=False),
        Binding("ctrl+right", "cursor_word_right", "Cursor word right", show=False),
        Binding("shift+up", "add_cursor_above", "Add cursor above", show=False),
        Binding("shift+down", "add_cursor_below", "Add cursor below", show=False),
        Binding("ctrl+shift+up", "cursor_up(True)", "Select up", show=False),
        Binding("ctrl+shift+down", "cursor_down(True)", "Select down", show=False),
        Binding("shift+home", "cursor_line_start(True)", "Select to line start", show=False),
        Binding("shift+end", "cursor_line_end(True)", "Select to line end", show=False),
        Binding("ctrl+shift+left", "cursor_word_left(True)", "Select word left", show=False),
        Binding("ctrl+shift+right", "cursor_word_right(True)", "Select word right", show=False),
        Binding("ctrl+a", "select_all", "Select all", show=False),
        Binding("ctrl+x", "cut", "Cut", show=False),
        Binding("ctrl+v", "paste", "Paste", show=False),
        Binding("ctrl+z", "undo", "Undo", show=False),
        Binding("ctrl+y,ctrl+shift+z", "redo", "Redo", show=False),
        Binding("ctrl+home", "cursor_document_start", "Document start", show=False),
        Binding("ctrl+end", "cursor_document_end", "Document end", show=False),
        Binding("ctrl+space", "completion_or_indent", "Complete SQL", show=False),
    ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._secondary_selections: list[Selection] = []
        self._cursor_edits: list[
            tuple[
                object,
                tuple[Selection, ...],
                tuple[Selection, ...],
                dict[Selection, int],
                dict[Selection, int],
            ]
        ] = []
        self._cursor_preferences: dict[Selection, int] = {}
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

    def load_text(self, text: str) -> None:
        """Release obsolete cursor state before Textual replaces the document."""
        self._secondary_selections.clear()
        self._cursor_preferences.clear()
        self._cursor_edits.clear()
        super().load_text(text)

    def _sync_horizontal_scrollbar(self) -> None:
        """TextArea scrolling must also update the scrollbar's numeric model."""
        if not self.is_mounted:
            return
        scrollbar = self.horizontal_scrollbar
        scrollbar.window_virtual_size = self.virtual_size.width
        scrollbar.window_size = self.scrollable_content_region.width
        scrollbar.position = int(self.scroll_x)

    def watch_scroll_x(self, old_value: float = 0.0, new_value: float = 0.0) -> None:
        super().watch_scroll_x(old_value, new_value)
        self._sync_horizontal_scrollbar()

    def on_idle(self, event: events.Idle | None = None) -> None:
        del event
        self._prune_cursor_history()
        if not self.is_mounted:
            return
        # Layout and document changes can update extents without a scroll event.
        self._sync_horizontal_scrollbar()
        app = cast("Any", self.app)
        workspace = app.active_workspace
        if workspace.editor is self and workspace.completion_menu.suggestions:
            app._position_completion(workspace)  # noqa: SLF001 -- tab-owned layout callback.

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
        return (min(selection), max(selection))

    def _set_selections(
        self,
        active: Selection,
        secondary: list[Selection],
        *,
        preferences: dict[Selection, int] | None = None,
    ) -> None:
        merged: list[tuple[bool, Selection]] = []
        resolved_preferences = dict(preferences or {})
        entries = [(True, active), *((False, item) for item in secondary)]
        for flag, selection in sorted(entries, key=lambda entry: self._selection_key(entry[1])):
            is_active, item = flag, selection
            low, high = sorted(item)
            if merged and low <= max(merged[-1][1]):
                previous_active, previous = merged.pop()
                low, high = min(low, *previous), max(high, *previous)
                direction = previous if previous_active else item
                item = (
                    Selection(high, low)
                    if direction.start > direction.end
                    else Selection(low, high)
                )
                resolved_preferences[item] = resolved_preferences.get(direction, direction.end[1])
                is_active = is_active or previous_active
            merged.append((is_active, item))
        active = next(item for is_active, item in merged if is_active)
        self._secondary_selections = [item for is_active, item in merged if not is_active]
        self._cursor_preferences = {
            item: resolved_preferences.get(item, item.end[1]) for _, item in merged
        }
        self.selection = active
        self.scroll_cursor_visible()
        self.refresh()
        self.post_message(self.SelectionChanged(self.selection, self))

    def collapse_to_active(self) -> None:
        if self._secondary_selections:
            self._secondary_selections = []
            self._cursor_preferences = {
                self.selection: self._cursor_preferences.get(self.selection, self.selection.end[1])
            }
            self.refresh()

    def _collapse_for_single_cursor_action(self) -> None:
        self.collapse_to_active()

    def action_add_cursor_above(self) -> None:
        self._add_cursor(-1)

    def action_add_cursor_below(self) -> None:
        self._add_cursor(1)

    def _add_cursor(self, direction: int) -> None:
        active = self.selection
        row = active.end[0]
        target_row = row + direction
        if target_row < 0 or target_row >= self.document.line_count:
            return
        column = self.logical_column
        target = Selection.cursor((target_row, min(column, len(self.document[target_row]))))
        existing = next(
            (selection for selection in self._secondary_selections if selection.end == target.end),
            None,
        )
        if existing is not None:
            self._secondary_selections.remove(existing)
            self._cursor_preferences.pop(existing, None)
            self.refresh()
            return
        self._set_selections(
            target,
            [*self._secondary_selections, active],
            preferences={**self._cursor_preferences, target: column},
        )

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

    def execution_text(self) -> str:
        """Return all selected SQL in document order, or the complete buffer."""
        return self.command_copy_text()

    def copy_to_clipboard(self) -> None:
        application = cast("SqlExplorerApp", self.app)
        if self.cursor_count == 1:
            application.copy_to_explorer_clipboard(self.command_copy_text())
            return
        fragments = tuple(
            self.document.get_text_range(*sorted(selection)) for selection in self.cursor_selections
        )
        value = "\n".join(fragments) if any(fragments) else ""
        application.copy_to_explorer_clipboard(value, fragments=fragments)

    def paste_clipboard(self, value: str) -> bool:
        application = cast("SqlExplorerApp", self.app)
        copied = application.clipboard_fragments(value)
        if not value:
            return False
        self._edit_cursors(value, fragments=paste_fragments(value, self.cursor_count, copied))
        self.focus()
        return True

    def _apply_batch_edit(
        self,
        insert: str,
        *,
        delete_left: bool = False,
        delete_right: bool = False,
    ) -> None:
        self._edit_cursors(insert, delete_left=delete_left, delete_right=delete_right)

    def _edit_cursors(
        self,
        insert: str,
        *,
        delete_left: bool = False,
        delete_right: bool = False,
        paired: bool = False,
        fragments: tuple[str, ...] | None = None,
    ) -> None:
        self._set_selections(
            self.selection, self._secondary_selections, preferences=self._cursor_preferences
        )
        before = (self.selection, *self._secondary_selections)
        before_preferences = dict(self._cursor_preferences)
        original = Document(self.text)
        entries = [(False, selection) for selection in self._secondary_selections]
        entries.append((True, self.selection))
        entries.sort(key=lambda entry: self._selection_key(entry[1]))
        edits: list[tuple[int, int, str, int, bool]] = []
        for index, (active, selection) in enumerate(entries):
            low, high = sorted(selection)
            start, end = (original.get_index_from_location(point) for point in (low, high))
            action = (
                "backspace"
                if delete_left
                else "delete"
                if delete_right
                else "paired"
                if paired
                else "insert"
            )
            insertion = fragments[index] if fragments is not None else insert
            start, end, value, caret = cursor_edit(self.text, (start, end), insertion, action)
            edits.append((start, end, value, caret, active))
        # Adjacent carets may expand into overlapping destructive ranges (for
        # example, deleting an empty pair). Delete the union only once.
        reconciled: list[tuple[int, int, str, int, bool]] = []
        for start, end, value, caret, active in sorted(edits):
            if reconciled and not value and not reconciled[-1][2] and start < reconciled[-1][1]:
                previous = reconciled.pop()
                reconciled.append(
                    (previous[0], max(previous[1], end), "", 0, active or previous[4])
                )
            else:
                reconciled.append((start, end, value, caret, active))
        edits = reconciled
        self.history.checkpoint()
        previous_edit = self._latest_undo_edit()
        ordered = sorted(edits)
        first, last = ordered[0][0], ordered[-1][1]
        chunks: list[str] = []
        offset = first
        for start, end, value, _, _ in ordered:
            chunks.extend((self.text[offset:start], value))
            offset = end
        replacement = "".join(chunks)
        if replacement != self.text[first:last]:
            self.replace(
                replacement,
                original.get_location_from_index(first),
                original.get_location_from_index(last),
                maintain_selection_offset=False,
            )
        self.history.checkpoint()
        result_document = Document(self.text)
        delta = 0
        positions: list[tuple[bool, Selection]] = []
        for start, end, value, caret, active in sorted(edits):
            positions.append(
                (
                    active,
                    Selection.cursor(
                        result_document.get_location_from_index(start + delta + caret)
                    ),
                )
            )
            delta += len(value) - (end - start)
        self._set_selections(
            next(point for active, point in positions if active),
            [point for active, point in positions if not active],
        )
        self._record_cursor_edit(previous_edit, before, before_preferences)

    def _latest_undo_edit(self) -> object | None:
        batches = self.history.undo_stack
        return batches[-1][-1] if batches and batches[-1] else None

    def _prune_cursor_history(self) -> None:
        """Keep snapshots only while their exact edits remain undoable or redoable."""
        retained = {
            id(edit)
            for batch in (*self.history.undo_stack, *self.history.redo_stack)
            for edit in batch
        }
        self._cursor_edits = [entry for entry in self._cursor_edits if id(entry[0]) in retained]

    def _record_cursor_edit(
        self,
        previous_edit: object | None,
        before: tuple[Selection, ...],
        before_preferences: dict[Selection, int],
    ) -> None:
        self._prune_cursor_history()
        edit = self._latest_undo_edit()
        if (
            edit is None
            or edit is previous_edit
            or any(recorded[0] is edit for recorded in self._cursor_edits)
        ):
            return
        self._cursor_edits.append(
            (
                edit,
                before,
                (self.selection, *self._secondary_selections),
                before_preferences,
                dict(self._cursor_preferences),
            )
        )

    def _history_cursors(self, edit: object, *, redo: bool) -> None:
        for recorded, before, after, before_preferences, after_preferences in reversed(
            self._cursor_edits
        ):
            if recorded is edit:
                selections = after if redo else before
                self._set_selections(
                    selections[0],
                    list(selections[1:]),
                    preferences=after_preferences if redo else before_preferences,
                )
                return
        self.collapse_to_active()

    def action_undo(self) -> None:
        edit = self._latest_undo_edit()
        super().action_undo()
        if edit is not None:
            self._history_cursors(edit, redo=False)
        self._prune_cursor_history()

    def action_redo(self) -> None:
        batches = self.history.redo_stack
        edit = batches[-1][-1] if batches and batches[-1] else None
        super().action_redo()
        if edit is not None:
            self._history_cursors(edit, redo=True)
        self._prune_cursor_history()

    @property
    def logical_column(self) -> int:
        return self._cursor_preferences.get(self.selection, self.cursor_location[1])

    def move_rows(self, amount: int, *, select: bool = False) -> None:
        last = self.document.line_count - 1
        moved: list[tuple[bool, Selection]] = []
        preferences: dict[Selection, int] = {}
        for active, selection in [
            (True, self.selection),
            *((False, item) for item in self._secondary_selections),
        ]:
            column = self._cursor_preferences.get(selection, selection.end[1])
            origin = selection.end
            if not select and not selection.is_empty:
                origin = min(selection) if amount < 0 else max(selection)
                column = origin[1]
            row = max(0, min(last, origin[0] + amount))
            target = (row, min(column, len(self.document[row])))
            item = Selection(selection.start, target) if select else Selection.cursor(target)
            moved.append((active, item))
            preferences[item] = column
        self._set_selections(
            next(item for active, item in moved if active),
            [item for active, item in moved if not active],
            preferences=preferences,
        )

    def add_cursors(self, count: int, direction: int) -> None:
        row = self.cursor_location[0]
        column = self.logical_column
        secondary = list(self._secondary_selections)
        preferences = dict(self._cursor_preferences)
        for distance in range(1, min(count, self.document.line_count) + 1):
            target = row + direction * distance
            if 0 <= target < self.document.line_count:
                item = Selection.cursor((target, min(column, len(self.document[target]))))
                secondary.append(item)
                preferences[item] = column
        self._set_selections(self.selection, secondary, preferences=preferences)

    def select_shortcut(self, *, default_catalog: str | None = None) -> bool:
        if self.cursor_count != 1 or not self.selection.is_empty:
            return False
        row, column = self.cursor_location
        line = self.document[row]
        if column != len(line) or not code_context(
            self.text, Document(self.text).get_index_from_location(self.cursor_location)
        ):
            return False
        match = re.fullmatch(r"([ \t]*)(select)([ \t]+\*)?[ \t]*", line, flags=re.IGNORECASE)
        if match is None:
            return False
        insertion = (
            "\n" + match[1] + "from " + (default_catalog + "." if default_catalog else "")
            if match[3]
            else " * "
        )
        self.history.checkpoint()
        result = self.replace(
            insertion,
            (row, len(line.rstrip())),
            (row, column),
            maintain_selection_offset=False,
        )
        self.history.checkpoint()
        self._set_selections(Selection.cursor(result.end_location), [])
        return True

    def _move_all(
        self,
        location_for: Any,
        *,
        select: bool = False,
        collapse_direction: int = 0,
    ) -> None:
        entries = [(False, selection) for selection in self._secondary_selections]
        entries.append((True, self.selection))
        moved: list[tuple[bool, Selection]] = []
        for active, selection in entries:
            target = (
                (min(selection) if collapse_direction < 0 else max(selection))
                if not select and not selection.is_empty and collapse_direction
                else location_for(selection.end)
            )
            moved.append(
                (active, Selection(selection.start, target) if select else Selection.cursor(target))
            )
        active_selection = next(selection for active, selection in moved if active)
        self._set_selections(
            active_selection, [selection for active, selection in moved if not active]
        )

    def action_cursor_up(self, select: bool = False) -> None:
        self.move_rows(-1, select=select)

    def action_cursor_down(self, select: bool = False) -> None:
        self.move_rows(1, select=select)

    def action_cursor_page_up(self) -> None:
        self.move_rows(-max(1, self.content_size.height))

    def action_cursor_page_down(self) -> None:
        self.move_rows(max(1, self.content_size.height))

    def _left_of(self, location: tuple[int, int]) -> tuple[int, int]:
        row, column = location
        if column:
            return row, column - 1
        return (row - 1, len(self.document[row - 1])) if row else location

    def _right_of(self, location: tuple[int, int]) -> tuple[int, int]:
        row, column = location
        if column < len(self.document[row]):
            return row, column + 1
        return (row + 1, 0) if row < self.document.line_count - 1 else location

    def _word_left_of(self, location: tuple[int, int]) -> tuple[int, int]:
        row, column = location
        if row > 0 and column == 0:
            return row - 1, len(self.document[row - 1])

        search_string = self.document[row][:column].rstrip()
        matches = list(re.finditer(self._word_pattern, search_string))
        return row, matches[-1].start() if matches else 0

    def _word_right_of(self, location: tuple[int, int]) -> tuple[int, int]:
        row, column = location
        line = self.document[row]
        if row < self.document.line_count - 1 and column == len(line):
            return row + 1, 0

        search_string = line[column:]
        stripped = search_string.lstrip()
        strip_offset = len(search_string) - len(stripped)
        matches = list(re.finditer(self._word_pattern, stripped))
        if not matches:
            return row, len(line)
        return row, column + strip_offset + matches[0].start()

    def action_cursor_left(self, select: bool = False) -> None:
        self._horizontal_move(-1, select=select)

    def action_cursor_right(self, select: bool = False) -> None:
        self._horizontal_move(1, select=select)

    def _horizontal_move(self, amount: int, *, select: bool = False) -> None:
        self._move_all(
            self._left_of if amount < 0 else self._right_of,
            select=select,
            collapse_direction=amount,
        )

    def action_cursor_word_left(self, select: bool = False) -> None:
        self._move_all(self._word_left_of, select=select, collapse_direction=-1)

    def action_cursor_word_right(self, select: bool = False) -> None:
        self._move_all(self._word_right_of, select=select, collapse_direction=1)

    def action_home(self) -> None:
        self.action_cursor_line_start()

    def action_cursor_line_start(self, select: bool = False) -> None:
        self._move_all(lambda location: (location[0], 0), select=select)

    def action_cursor_line_end(self, select: bool = False) -> None:
        self._move_all(
            lambda location: (location[0], len(self.document[location[0]])), select=select
        )

    def action_cursor_document_start(self, select: bool = False) -> None:
        self._move_all(lambda _location: (0, 0), select=select)

    def action_cursor_document_end(self, select: bool = False) -> None:
        last_row = self.document.line_count - 1
        self._move_all(lambda _location: (last_row, len(self.document[last_row])), select=select)

    def action_select_all(self) -> None:
        self._collapse_for_single_cursor_action()
        super().action_select_all()

    def action_delete_left(self) -> None:
        self._apply_batch_edit("", delete_left=True)

    def action_delete_right(self) -> None:
        self._apply_batch_edit("", delete_right=True)

    def action_cut(self) -> None:
        if self.cursor_count > 1 or not self.selection.is_empty:
            self.copy_to_clipboard()
            if any(not selection.is_empty for selection in self.cursor_selections):
                self._apply_batch_edit("")

    def action_paste(self) -> None:
        self.paste_clipboard(cast("SqlExplorerApp", self.app).paste_from_explorer_clipboard())

    async def _on_paste(self, event: events.Paste) -> None:
        if not self.read_only:
            self.paste_clipboard(event.text)

    async def _on_key(self, event: events.Key) -> None:
        application = cast("SqlExplorerApp", self.app)
        from .workspace import workspace_for  # noqa: PLC0415 -- avoids widget import cycle.

        if event.key.split("+")[-1] == "tab":
            event.stop()
            event.prevent_default()
            if event.key == "tab":
                application.action_plain_tab()
            elif event.key == "shift+tab":
                application.action_plain_shift_tab()
            return
        menu = workspace_for(self).completion_menu
        if menu.is_open and event.key in {"up", "down", "enter", "escape"}:
            event.stop()
            event.prevent_default()
            if event.key == "enter":
                application.action_complete()
            elif event.key == "escape":
                menu.action_close()
            else:
                menu.move_highlight(-1 if event.key == "up" else 1)
            return
        if event.key == "ctrl+space":
            event.stop()
            event.prevent_default()
            application.action_complete()
            return
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            application.action_escape()
            return
        if not self.read_only and (event.is_printable or event.key == "enter"):
            event.stop()
            event.prevent_default()
            self._edit_cursors(
                "\n" if event.key == "enter" else (event.character or ""),
                paired=event.is_printable,
            )
            return
        await super()._on_key(event)

    async def on_event(self, event: events.Event) -> None:
        if isinstance(event, events.MouseEvent) and getattr(self.app, "keyboard_mode", False):
            event.stop()
            event.prevent_default()
            return
        await super().on_event(event)

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        self._collapse_for_single_cursor_action()
        await super()._on_mouse_down(event)
        # A click is a new column choice even when the clamped position is unchanged.
        self._cursor_preferences = {self.selection: self.selection.end[1]}

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

    def render_line(self, y: int) -> Strip:
        # Textual's native cursor is replaced by the block caret overlay below.
        theme = self._theme
        if theme is not None:
            self._theme = cast("Any", _CaretRenderTheme(theme))
        try:
            strip = super().render_line(y)
        finally:
            self._theme = theme
        # Apply find colors after Textual's primary selection/cursor rendering.
        for start, end in self._search_matches:
            start_x, start_y = self.wrapped_document.location_to_offset(start)
            end_x, end_y = self.wrapped_document.location_to_offset(end)
            visual_row = y + int(self.scroll_y)
            if start_y <= visual_row <= end_y:
                left = (
                    max(self.gutter_width, start_x - int(self.scroll_x) + self.gutter_width)
                    if visual_row == start_y
                    else self.gutter_width
                )
                right = (
                    min(strip.cell_length, end_x - int(self.scroll_x) + self.gutter_width)
                    if visual_row == end_y
                    else strip.cell_length
                )
                if left < right:
                    strip = Strip.join(
                        [
                            strip.crop(0, left),
                            Strip(
                                Segment(
                                    segment.text,
                                    (segment.style or Style()) + _SEARCH_MATCH_STYLE,
                                    segment.control,
                                )
                                for segment in strip.crop(left, right)
                            ),
                            strip.crop(right),
                        ]
                    )
        # Block carets, like the command pane input's cursor: the character cell at
        # each caret (every cell of an expanded tab or wide character) is drawn in
        # the input cursor colors. No cell is inserted, so text never shifts.
        caret_style = self.get_component_rich_style(CARET_COMPONENT)
        visual_row = y + int(self.scroll_y)
        for selection in self.cursor_selections:
            row, column = selection.end
            # Secondary carets can briefly trail the document during a batch edit.
            if row >= self.document.line_count:
                continue
            cursor_x, cursor_y = self.wrapped_document.location_to_offset(selection.end)
            if cursor_y != visual_row:
                continue
            x = cursor_x - int(self.scroll_x) + self.gutter_width
            width = 1
            if column < len(self.document[row]):
                next_x, next_y = self.wrapped_document.location_to_offset((row, column + 1))
                # When the next character wraps, this character fills the remaining
                # visible cells. In particular, a tab must not shrink to one cell.
                width = (
                    max(1, next_x - cursor_x)
                    if next_y == cursor_y
                    else max(1, strip.cell_length - x)
                )
            left = max(self.gutter_width, x)
            right = min(strip.cell_length, x + width)
            if left < right:
                caret = Strip(
                    Segment(segment.text, (segment.style or Style()) + caret_style, segment.control)
                    for segment in strip.crop(left, right)
                )
                strip = Strip.join([strip.crop(0, left), caret, strip.crop(right)])
        return strip.adjust_cell_length(self.scrollable_content_region.width, self.rich_style)

    def _selection_style(self) -> Style:
        theme = self._theme
        if theme is not None and theme.selection_style is not None:
            return theme.selection_style
        return self.get_component_rich_style("text-area--selection")

    def get_line(self, line_index: int) -> Text:
        # TextArea owns the document. All padding and overlays are presentation only.
        line = super().get_line(line_index)
        selections = self._secondary_selections
        if not self.has_focus:
            selections = [*selections, self.selection]
        for selection in selections:
            start, end = sorted(selection)
            if start[0] <= line_index <= end[0] and start != end:
                line.stylize(
                    self._selection_style(),
                    start[1] if line_index == start[0] else 0,
                    end[1] if line_index == end[0] else len(line),
                )
        # Search colors take precedence over selection colors.
        for (start_row, start_column), (end_row, end_column) in self._search_matches:
            if start_row == line_index == end_row:
                line.stylize(_SEARCH_MATCH_STYLE, start_column, end_column)
        return line

    def action_completion_or_indent(self) -> None:
        if self.cursor_count > 1:
            self.action_indent()
            return
        cast("SqlExplorerApp", self.app).action_complete()

    def action_indent(self) -> None:
        if all(selection.is_empty for selection in self.cursor_selections):
            self._apply_batch_edit(_INDENT)
        else:
            self._indent_rows(unindent=False)

    def action_unindent(self) -> None:
        self._indent_rows(unindent=True)

    def _indent_rows(self, *, unindent: bool) -> None:
        before = (self.selection, *self._secondary_selections)
        before_preferences = dict(self._cursor_preferences)
        rows: set[int] = set()
        for selection in before:
            low, high = sorted(selection)
            last = high[0] - int(high[1] == 0 and high[0] > low[0])
            rows.update(range(low[0], last + 1))
        amounts: dict[int, int] = {}
        for row in rows:
            line = self.document[row]
            if unindent:
                amount = min(len(line) - len(line.lstrip(" ")), len(_INDENT))
                empty_columns = [
                    item.end[1] for item in before if item.is_empty and item.end[0] == row
                ]
                if empty_columns:
                    amount = min(amount, *empty_columns)
                amounts[row] = -amount
            else:
                amounts[row] = len(_INDENT)
        if not any(amounts.values()):
            self._prune_cursor_history()
            return
        first, last = min(rows), max(rows)
        original = Document(self.text)
        lines = original.lines[first : last + 1]
        for index, row in enumerate(range(first, last + 1)):
            amount = amounts.get(row, 0)
            lines[index] = _INDENT + lines[index] if amount > 0 else lines[index][-amount:]
        self.history.checkpoint()
        previous_edit = self._latest_undo_edit()
        self.replace(
            "\n".join(lines),
            (first, 0),
            (last, len(original[last])),
            maintain_selection_offset=False,
        )
        self.history.checkpoint()

        def translate(point: tuple[int, int]) -> tuple[int, int]:
            row, column = point
            return row, max(0, column + amounts.get(row, 0))

        after = tuple(Selection(translate(item.start), translate(item.end)) for item in before)
        if not unindent and len(before) == 1 and not before[0].is_empty:
            low, high = sorted(after[0])
            low = (low[0], 0)
            after = (
                Selection(high, low) if before[0].start > before[0].end else Selection(low, high),
            )
        preferences = {
            new: max(0, before_preferences.get(old, old.end[1]) + amounts.get(old.end[0], 0))
            for old, new in zip(before, after)
        }
        self._set_selections(after[0], list(after[1:]), preferences=preferences)
        self._record_cursor_edit(previous_edit, before, before_preferences)
