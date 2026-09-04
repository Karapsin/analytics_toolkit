from __future__ import annotations

from decimal import Decimal
from numbers import Integral, Real
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional, cast

import pandas as pd
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, OptionList, Static, Tree

from .editor import SqlEditor
from .filetree import completion_entries, safe_entries
from .inputs import EditableInput
from .panes import CommandInput, ResultMessage
from .scrollbars import LeftVerticalScrollbarMixin

if TYPE_CHECKING:
    from rich.style import Style
    from textual import events
    from textual.app import ComposeResult

    from .app import SqlExplorerApp
    from .statements import ExplorerExecutionPlan

_MAX_CELL_LENGTH = 512
_MAX_CONFIRMATION_PREVIEW_LENGTH = 2_000


class ResultTable(LeftVerticalScrollbarMixin, DataTable[Any]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "cursor_up", "Cursor up", show=False),
        Binding("down", "cursor_down", "Cursor down", show=False),
        Binding("left", "cursor_left", "Cursor left", show=False),
        Binding("right", "cursor_right", "Cursor right", show=False),
        Binding("shift+up", "cursor_up(True)", "Extend up", show=False),
        Binding("shift+down", "cursor_down(True)", "Extend down", show=False),
        Binding("shift+left", "cursor_left(True)", "Extend left", show=False),
        Binding("shift+right", "cursor_right(True)", "Extend right", show=False),
        Binding("delete", "close_results", "Close results", show=False),
    ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.selection_anchor: tuple[int, int] | None = None
        self.selected_cells: tuple[tuple[int, int], tuple[int, int]] | None = None
        self.selected_header: str | None = None
        self.selected_header_index: int | None = None
        self._dragging = False
        self._drag_moved = False

    def set_cell_selection(self, row: int, column: int, *, extend: bool = False) -> None:
        if not extend or self.selection_anchor is None:
            self.selection_anchor = (row, column)
        anchor = self.selection_anchor
        self.selected_cells = (anchor, (row, column))
        self.selected_header = None
        self.selected_header_index = None
        self.cursor_coordinate = Coordinate(row, column)
        self._refresh_selection()

    def select_header(self, label: str, column: int | None = None) -> None:
        self.selection_anchor = None
        self.selected_header = label
        self.selected_header_index = column
        self.selected_cells = None
        self._refresh_selection()

    def clear_rectangular_selection(self) -> None:
        self.selection_anchor = None
        self.selected_cells = None
        self.selected_header = None
        self.selected_header_index = None
        self._refresh_selection()

    def copy_text(self) -> str:
        if self.selected_header is not None:
            return self.selected_header
        if self.selected_cells is None:
            row, column = self.cursor_coordinate
            return str(self.get_cell_at(Coordinate(row, column)))
        (row_a, col_a), (row_b, col_b) = self.selected_cells
        rows = range(min(row_a, row_b), max(row_a, row_b) + 1)
        cols = range(min(col_a, col_b), max(col_a, col_b) + 1)
        return "\n".join(
            "\t".join(str(self.get_cell_at(Coordinate(r, c))) for c in cols) for r in rows
        )

    def action_cursor_up(self, select: bool = False) -> None:  # noqa: FBT001,FBT002
        if select:
            self._extend_by(-1, 0)
            return
        if self.selected_header is not None:
            self.clear_rectangular_selection()
            cast("SqlExplorerApp", self.app).action_focus_previous_pane()
            return
        if not self.columns:
            cast("SqlExplorerApp", self.app).action_focus_previous_pane()
            return
        if self.row_count == 0 or self.cursor_row == 0:
            column = self.cursor_column
            self.select_header(str(self.ordered_columns[column].label), column)
            return
        self.clear_rectangular_selection()
        super().action_cursor_up()

    def action_cursor_down(self, select: bool = False) -> None:  # noqa: FBT001,FBT002
        if select:
            self._extend_by(1, 0)
            return
        if self.selected_header is not None:
            column = self.selected_header_index if self.selected_header_index is not None else 0
            if self.row_count:
                self.set_cell_selection(0, column)
            else:
                cast("SqlExplorerApp", self.app).action_focus_next_pane()
            return
        if self.row_count == 0 or self.cursor_row >= self.row_count - 1:
            cast("SqlExplorerApp", self.app).action_focus_next_pane()
            return
        self.clear_rectangular_selection()
        super().action_cursor_down()

    def action_cursor_left(self, select: bool = False) -> None:  # noqa: FBT001,FBT002
        if select:
            self._extend_by(0, -1)
            return
        if self.selected_header is not None:
            column = self.selected_header_index if self.selected_header_index is not None else 0
            column = max(0, column - 1)
            self.select_header(str(self.ordered_columns[column].label), column)
            return
        self.clear_rectangular_selection()
        super().action_cursor_left()

    def action_cursor_right(self, select: bool = False) -> None:  # noqa: FBT001,FBT002
        if select:
            self._extend_by(0, 1)
            return
        if self.selected_header is not None:
            column = self.selected_header_index if self.selected_header_index is not None else 0
            column = min(len(self.columns) - 1, column + 1)
            self.select_header(str(self.ordered_columns[column].label), column)
            return
        self.clear_rectangular_selection()
        super().action_cursor_right()

    def _extend_by(self, rows: int, columns: int) -> None:
        column_count = len(self.columns)
        if not self.row_count or not column_count:
            return
        row, column = self.cursor_coordinate
        row = min(max(0, row + rows), self.row_count - 1)
        column = min(max(0, column + columns), column_count - 1)
        self.set_cell_selection(row, column, extend=True)

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        coordinate = self._event_coordinate(event)
        if coordinate is None:
            return
        row, column = coordinate
        if row >= 0 and column >= 0:
            self.set_cell_selection(row, column, extend=event.shift)
            self._dragging = True
            self._drag_moved = False
            self.capture_mouse()
            self.focus()
            event.stop()

    def _on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            super()._on_mouse_move(event)
            return
        coordinate = self._event_coordinate(event)
        if coordinate is not None and coordinate[0] >= 0 and coordinate[1] >= 0:
            self.set_cell_selection(*coordinate, extend=True)
            self._drag_moved = True
            event.stop()

    async def _on_mouse_up(self, event: events.MouseUp) -> None:
        if self._dragging:
            self._dragging = False
            self.release_mouse()
            event.stop()

    async def _on_click(self, event: events.Click) -> None:
        coordinate = self._event_coordinate(event)
        if coordinate is None:
            return
        row, column = coordinate
        if row == -1 and column >= 0:
            label = str(self.ordered_columns[column].label)
            self.select_header(label, column)
            self.focus()
            event.stop()
            return
        if row >= 0 and column >= 0:
            if not self._drag_moved:
                self.set_cell_selection(row, column, extend=event.shift)
            self._drag_moved = False
            self.focus()
            event.stop()

    @staticmethod
    def _event_coordinate(event: events.MouseEvent) -> tuple[int, int] | None:
        meta = event.style.meta
        if "row" not in meta or "column" not in meta:
            return None
        return int(meta["row"]), int(meta["column"])

    def _render_cell(  # noqa: PLR0913
        self,
        row_index: int,
        column_index: int,
        base_style: Style,
        width: int,
        cursor: bool = False,  # noqa: FBT001,FBT002
        hover: bool = False,  # noqa: FBT001,FBT002
    ) -> Any:
        selected = self._is_selected(row_index, column_index)
        return super()._render_cell(
            row_index,
            column_index,
            base_style,
            width,
            cursor=cursor or selected,
            hover=hover,
        )

    def _is_selected(self, row: int, column: int) -> bool:
        if row == -1:
            return column == self.selected_header_index
        if column < 0 or self.selected_cells is None:
            return False
        (row_a, col_a), (row_b, col_b) = self.selected_cells
        return min(row_a, row_b) <= row <= max(row_a, row_b) and min(col_a, col_b) <= column <= max(
            col_a, col_b
        )

    def _refresh_selection(self) -> None:
        self._cell_render_cache.clear()
        self.refresh()

    def action_close_results(self) -> None:
        cast("SqlExplorerApp", self.app).close_results()


class SqlFileTree(Tree[object]):
    """A read-only tree; it deliberately exposes no filesystem mutations."""

    class DirectoryError(Message):
        def __init__(self, error: OSError) -> None:
            super().__init__()
            self.error = error

    def __init__(self, root: Any = None, *args: Any, **kwargs: Any) -> None:
        self.root_path = Path(root or Path.cwd()).resolve()
        super().__init__(self.root_path.name or str(self.root_path), *args, **kwargs)
        self.auto_expand = False

    def on_mount(self) -> None:
        self.refresh_directory(self.root, self.root_path)
        self.root.expand()

    def refresh_directory(self, node: Any, path: Any) -> None:
        try:
            entries = safe_entries(path, browse_root=self.root_path)
        except OSError as exc:
            self.post_message(self.DirectoryError(exc))
            return
        node.remove_children()
        for entry in entries:
            node.add(entry.name, data=entry, allow_expand=entry.is_dir())

    def show_entries(self, directory: Path, entries: tuple[Path, ...]) -> None:
        self.move_cursor(None)
        self.reset(directory.name or str(directory), data=directory)
        self.root.expand()
        for entry in entries:
            self.root.add(entry.name, data=entry, allow_expand=entry.is_dir())


class FileNavigationScreen(ModalScreen[Optional[Path]]):
    """Browse SQL files, or choose a destination directory for a new file."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close navigation", show=False),
        Binding("tab", "complete_path", "Complete path", show=False, priority=True),
        Binding("shift+tab", "previous_match", "Previous match", show=False, priority=True),
        Binding("up", "previous_match", "Previous match", show=False, priority=True),
        Binding("down", "next_match", "Next match", show=False, priority=True),
        Binding("enter", "choose_path", "Open or descend", show=False, priority=True),
    ]

    CSS = """
    FileNavigationScreen {
        align: center middle;
    }
    #navigation-dialog {
        width: 80%;
        height: 80%;
        max-width: 120;
        border: round $accent;
        background: $panel;
        padding: 1;
    }
    #navigation-title, #navigation-notice, #navigation-help {
        height: 1;
    }
    #navigation-path {
        height: 3;
        margin-bottom: 1;
    }
    #navigation-tree {
        height: 1fr;
        border: round $panel-lighten-2;
    }
    #navigation-tree:focus {
        border: double $accent;
        background: $panel-lighten-1;
    }
    """

    def __init__(self, root: Path | None = None, *, select_directory: bool = False) -> None:
        super().__init__()
        self.root_path = (root or Path.cwd()).resolve()
        self.select_directory = select_directory
        self._directory = self.root_path
        self._matches: tuple[Path, ...] = ()
        self._match_index = -1
        self._directory_confirmation_armed = False

    def compose(self) -> ComposeResult:
        with Vertical(id="navigation-dialog"):
            action = "Select destination directory" if self.select_directory else "Open SQL file"
            yield Static(f"{action} — {self.root_path}", id="navigation-title")
            yield EditableInput(
                placeholder="Type a path; Tab completes",
                id="navigation-path",
            )
            yield SqlFileTree(self.root_path, id="navigation-tree")
            if self.select_directory:
                yield Button("Select this directory", id="navigation-select-directory")
            yield Static("", id="navigation-notice")
            yield Static(
                "Tab: complete/cycle   ↑↓: choose   Enter/click: open   Escape: cancel"
                if not self.select_directory
                else "Tab: complete/cycle   Enter: select typed directory   Click: descend",
                id="navigation-help",
            )

    def on_mount(self) -> None:
        self._refresh_matches("")
        self.query_one("#navigation-path", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "navigation-path":
            self._clear_directory_confirmation()
            self._refresh_matches(event.value)

    def on_tree_node_selected(self, event: Tree.NodeSelected[object]) -> None:
        path = event.node.data
        if isinstance(path, Path):
            event.stop()
            self._clear_directory_confirmation()
            self._choose_entry(path)

    def action_complete_path(self) -> None:
        if not self._matches:
            self._refresh_matches(self.query_one("#navigation-path", Input).value)
            if not self._matches:
                return
        if len(self._matches) == 1:
            path = self._matches[0]
            try:
                is_directory = path.is_dir()
            except OSError as exc:
                self._set_navigation_notice(f"{type(exc).__name__}: {exc}")
                return
            if is_directory:
                self._descend(path)
            else:
                self._set_path_value(self._display_path(path))
                self._set_navigation_notice("Path completed; press Enter to open it.")
            return
        self._highlight_match(self._match_index + 1)

    def action_previous_match(self) -> None:
        if self._directory_confirmation_armed:
            self._resume_directory_selection()
        self._highlight_match(self._match_index - 1 if self._match_index >= 0 else -1)

    def action_next_match(self) -> None:
        if self._directory_confirmation_armed:
            self._resume_directory_selection()
        self._highlight_match(self._match_index + 1)

    def action_choose_path(self) -> None:
        if self._directory_confirmation_armed:
            self.action_choose_directory()
            return
        if self.select_directory and self.query_one("#navigation-path", Input).value.endswith("/"):
            self.dismiss(self._directory)
            return
        if not self._matches:
            self._set_navigation_notice("No matching path.")
            return
        exact_name = Path(self.query_one("#navigation-path", Input).value.rstrip("/")).name
        exact = next(
            (path for path in self._matches if path.name.casefold() == exact_name.casefold()),
            None,
        )
        index = max(self._match_index, 0)
        self._choose_entry(exact or self._matches[index])

    def _refresh_matches(self, value: str) -> None:
        tree = self.query_one(SqlFileTree)
        try:
            directory, matches = completion_entries(self.root_path, value)
        except (OSError, ValueError) as exc:
            self._matches = ()
            self._match_index = -1
            tree.show_entries(self.root_path, ())
            self._set_navigation_notice(f"{type(exc).__name__}: {exc}")
            return
        self._matches = matches
        self._match_index = -1
        self._directory = directory
        tree.show_entries(directory, matches)
        self._set_navigation_notice(
            f"{len(matches)} matching entr{'y' if len(matches) == 1 else 'ies'}."
        )

    def _highlight_match(self, index: int) -> None:
        if not self._matches:
            self._set_navigation_notice("No matching path.")
            return
        self._match_index = index % len(self._matches)
        tree = self.query_one(SqlFileTree)
        node = tree.root.children[self._match_index]
        tree.move_cursor(node)
        self._set_navigation_notice(str(self._matches[self._match_index].name))
        self.query_one("#navigation-path", Input).focus()

    def _choose_entry(self, path: Path) -> None:
        try:
            is_directory = path.is_dir()
        except OSError as exc:
            self._set_navigation_notice(f"{type(exc).__name__}: {exc}")
            return
        if is_directory:
            self._descend(path)
        elif self.select_directory:
            self._set_navigation_notice("Choose a directory, then select it with the button.")
            self.query_one("#navigation-path", Input).focus()
        elif path.suffix.casefold() == ".sql":
            self.dismiss(path)
        else:
            self._set_navigation_notice("Only .sql files can be opened.")
            self.query_one("#navigation-path", Input).focus()

    def action_choose_directory(self) -> None:
        if not self.select_directory:
            return
        self.dismiss(self._directory)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "navigation-select-directory":
            self.action_choose_directory()

    def _descend(self, path: Path) -> None:
        self._clear_directory_confirmation()
        value = self._display_path(path) + "/"
        self._set_path_value(value)
        self._refresh_matches(value)

    def _set_path_value(self, value: str) -> None:
        path_input = self.query_one("#navigation-path", Input)
        path_input.value = value
        path_input.cursor_position = len(value)
        path_input.focus()

    def _display_path(self, path: Path) -> str:
        return path.relative_to(self.root_path).as_posix()

    def _set_navigation_notice(self, message: str) -> None:
        self.query_one("#navigation-notice", Static).update(message)

    def on_sql_file_tree_directory_error(
        self,
        event: SqlFileTree.DirectoryError,
    ) -> None:
        event.stop()
        message = f"{type(event.error).__name__}: {event.error}"
        if not self.is_active:
            return
        self.dismiss(result=None)
        self.app.call_after_refresh(
            cast("SqlExplorerApp", self.app).show_message,
            message,
        )

    def action_cancel(self) -> None:
        if self.select_directory and not self._directory_confirmation_armed:
            self._arm_directory_confirmation()
            return
        self.dismiss(result=None)

    def _arm_directory_confirmation(self) -> None:
        self._directory_confirmation_armed = True
        button = self.query_one("#navigation-select-directory", Button)
        button.add_class("armed")
        button.focus()
        self._set_navigation_notice(
            "Enter: select this directory   Escape: cancel   Arrows: keep browsing"
        )

    def _clear_directory_confirmation(self) -> None:
        if not self._directory_confirmation_armed:
            return
        self._directory_confirmation_armed = False
        self.query_one("#navigation-select-directory", Button).remove_class("armed")

    def _resume_directory_selection(self) -> EditableInput:
        self._clear_directory_confirmation()
        path_input = self.query_one("#navigation-path", EditableInput)
        path_input.focus_preserving_cursor()
        self._set_navigation_notice(
            f"{len(self._matches)} matching entr{'y' if len(self._matches) == 1 else 'ies'}."
        )
        return path_input

    async def _on_key(self, event: events.Key) -> None:
        key_parts = event.key.split("+")
        key = key_parts[-1]
        if self._directory_confirmation_armed and key in {"left", "right"}:
            path_input = self._resume_directory_selection()
            select = "shift" in key_parts
            word = "ctrl" in key_parts
            if key == "left":
                action = (
                    path_input.action_cursor_left_word if word else path_input.action_cursor_left
                )
                self.app.call_after_refresh(action, select)
            else:
                action = (
                    path_input.action_cursor_right_word if word else path_input.action_cursor_right
                )
                self.app.call_after_refresh(action, select)
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


class CompletionMenu(OptionList):
    """Cursor-adjacent SQL completion overlay."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close completion", show=False),
        Binding("tab", "accept", "Accept completion", show=False),
    ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.suggestions: tuple[str, ...] = ()

    @property
    def is_open(self) -> bool:
        return self.styles.display != "none"

    def open(self, suggestions: tuple[str, ...]) -> None:
        self.suggestions = suggestions
        self.clear_options()
        self.add_options(suggestions)
        self.highlighted = 0 if suggestions else None
        self.styles.display = "block" if suggestions else "none"

    def move_highlight(self, offset: int) -> None:
        if not self.suggestions:
            return
        current = self.highlighted if self.highlighted is not None else 0
        self.highlighted = max(0, min(current + offset, len(self.suggestions) - 1))

    def selected_suggestion(self) -> str | None:
        if self.highlighted is None:
            return None
        return self.suggestions[self.highlighted]

    def action_close(self) -> None:
        from .workspace import workspace_for  # noqa: PLC0415 -- avoids widget import cycle.

        self.styles.display = "none"
        self.suggestions = ()
        workspace_for(self).editor.focus()

    def action_accept(self) -> None:
        cast("SqlExplorerApp", self.app).action_plain_tab()


class FindReplaceBar(Vertical):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close find", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield EditableInput(placeholder="Find", id="find-pattern")
        yield EditableInput(placeholder="Replace", id="replace-pattern")
        yield Button("Next", id="find-next")
        with Horizontal(id="replace-actions"):
            yield Button("Replace", id="replace-current")
            yield Button("Replace All", id="replace-all")

    def open(self) -> None:
        from .workspace import workspace_for  # noqa: PLC0415 -- avoids widget import cycle.

        editor = workspace_for(self).editor
        search_input = self.query_one("#find-pattern", Input)
        selected = editor.selected_text
        if selected and "\n" not in selected:
            search_input.value = selected
        self.styles.display = "block"
        editor.set_search_pattern(search_input.value)
        search_input.focus()
        cast("SqlExplorerApp", self.app).enable_find_navigation()
        self._update_notice()

    @property
    def is_open(self) -> bool:
        return self.styles.display != "none"

    def focus_relative(self, direction: int) -> None:
        """Move focus through the visible Find/Replace controls."""
        controls = (
            self.query_one("#find-pattern", Input),
            self.query_one("#replace-pattern", Input),
            self.query_one("#find-next", Button),
            self.query_one("#replace-current", Button),
            self.query_one("#replace-all", Button),
        )
        focused = self.app.focused
        try:
            index = controls.index(focused)
        except ValueError:
            controls[0 if direction > 0 else -1].focus()
            return
        controls[(index + direction) % len(controls)].focus()

    def action_close(self) -> None:
        from .workspace import workspace_for  # noqa: PLC0415 -- avoids widget import cycle.

        self.styles.display = "none"
        cast("SqlExplorerApp", self.app).disable_find_navigation()
        editor = workspace_for(self).editor
        editor.clear_search()
        editor.focus()

    def find_next(self) -> None:
        from .workspace import workspace_for  # noqa: PLC0415 -- avoids widget import cycle.

        position = workspace_for(self).editor.select_next_search_match()
        self._update_notice(position)

    def replace_current(self) -> None:
        from .workspace import workspace_for  # noqa: PLC0415 -- avoids widget import cycle.

        editor = workspace_for(self).editor
        replacement = self.query_one("#replace-pattern", Input).value
        editor.replace_current_search_match(replacement)
        self._update_notice(editor.search_position)

    def replace_all(self) -> None:
        from .workspace import workspace_for  # noqa: PLC0415 -- avoids widget import cycle.

        editor = workspace_for(self).editor
        replacement = self.query_one("#replace-pattern", Input).value
        count = editor.replace_all_search_matches(replacement)
        cast("SqlExplorerApp", self.app).show_notice(
            f"Replaced {count} occurrence(s).",
            workspace_for(self),
        )

    def _update_notice(self, position: int | None = None) -> None:
        from .workspace import workspace_for  # noqa: PLC0415 -- avoids widget import cycle.

        editor = workspace_for(self).editor
        pattern = self.query_one("#find-pattern", Input).value
        if not pattern:
            message = "Enter text to find."
        elif not editor.search_match_count:
            message = "No matches found."
        elif position is None:
            message = f"Found {editor.search_match_count} occurrence(s)."
        else:
            message = f"Match {position} of {editor.search_match_count}."
        cast("SqlExplorerApp", self.app).show_notice(message, workspace_for(self))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "find-pattern":
            self.find_next()
        elif event.input.id == "replace-pattern":
            self.replace_current()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "find-pattern":
            return
        from .workspace import workspace_for  # noqa: PLC0415 -- avoids widget import cycle.

        workspace_for(self).editor.set_search_pattern(event.value)
        self._update_notice()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "find-next":
            self.find_next()
        elif event.button.id == "replace-current":
            self.replace_current()
        elif event.button.id == "replace-all":
            self.replace_all()


class ConfirmMutationScreen(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left", "select_confirm", "Select execute", show=False, priority=True),
        Binding("right", "select_cancel", "Select cancel", show=False, priority=True),
        Binding("y", "confirm", "Execute", show=False),
        Binding("n,escape", "cancel", "Cancel", show=False),
    ]

    CSS = """
    ConfirmMutationScreen {
        align: center middle;
    }
    #confirmation-dialog {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        border: round $warning;
        background: $panel;
        padding: 1 2;
    }
    #confirmation-preview {
        height: auto;
        max-height: 16;
        overflow-y: auto;
        margin: 1 0;
    }
    #confirmation-buttons {
        height: 3;
        align-horizontal: center;
    }
    """

    def __init__(self, plan: ExplorerExecutionPlan, *, db_key: str, backend: str) -> None:
        super().__init__()
        self.plan = plan
        self.db_key = db_key
        self.backend = backend

    def compose(self) -> ComposeResult:
        preview = self.plan.execution_sql
        if len(preview) > _MAX_CONFIRMATION_PREVIEW_LENGTH:
            preview = preview[: _MAX_CONFIRMATION_PREVIEW_LENGTH - 3] + "..."
        with Vertical(id="confirmation-dialog"):
            yield Static(
                f"Execute {self.plan.statement_count} non-read statement(s) on "
                f"{self.db_key} ({self.backend})?",
                markup=False,
            )
            yield Static(preview, id="confirmation-preview", markup=False)
            with Horizontal(id="confirmation-buttons"):
                yield Button("Execute [Y]", variant="warning", id="confirm-execute")
                yield Button("Cancel [N]", id="confirm-cancel")

    def action_confirm(self) -> None:
        self.dismiss(result=True)

    def action_cancel(self) -> None:
        self.dismiss(result=False)

    def action_select_confirm(self) -> None:
        self.query_one("#confirm-execute", Button).focus()

    def action_select_cancel(self) -> None:
        self.query_one("#confirm-cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-execute")


class DiscardChangesScreen(ModalScreen[bool]):
    """Confirm replacing an editor buffer that has unsaved changes."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left", "select_discard", "Select discard", show=False, priority=True),
        Binding("right", "select_cancel", "Select cancel", show=False, priority=True),
        Binding("y", "discard", "Discard", show=False),
        Binding("n,escape", "cancel", "Keep editing", show=False),
    ]

    CSS = """
    DiscardChangesScreen {
        align: center middle;
    }
    #discard-dialog {
        width: 70%;
        max-width: 80;
        height: auto;
        border: round $warning;
        background: $panel;
        padding: 1 2;
    }
    #discard-buttons {
        height: 3;
        align-horizontal: center;
    }
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    def compose(self) -> ComposeResult:
        with Vertical(id="discard-dialog"):
            yield Static(
                f"Discard unsaved changes and open {self.path}?",
                markup=False,
            )
            with Horizontal(id="discard-buttons"):
                yield Button("Discard [Y]", variant="warning", id="discard-confirm")
                yield Button("Keep editing [N]", id="discard-cancel")

    def action_discard(self) -> None:
        self.dismiss(result=True)

    def action_cancel(self) -> None:
        self.dismiss(result=False)

    def action_select_discard(self) -> None:
        self.query_one("#discard-confirm", Button).focus()

    def action_select_cancel(self) -> None:
        self.query_one("#discard-cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "discard-confirm")


def _format_cell(value: object) -> str:
    if value is None:
        return "NULL"
    try:
        if bool(pd.isna(value)):
            return "NULL"
    except (TypeError, ValueError):
        pass
    if isinstance(value, Decimal) and value.is_finite():
        normalized = Decimal(0) if value == 0 else value.normalize()
        rendered = format(normalized, ",f")
    elif isinstance(value, Integral) and not isinstance(value, bool):
        rendered = format(value, ",d")
    elif isinstance(value, Real) and not isinstance(value, bool):
        rendered = format(value, ",")
    else:
        rendered = str(value)
    rendered = (
        rendered.replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    if len(rendered) > _MAX_CELL_LENGTH:
        return rendered[: _MAX_CELL_LENGTH - 1] + "…"
    return rendered


__all__ = [
    "CommandInput",
    "CompletionMenu",
    "ConfirmMutationScreen",
    "DiscardChangesScreen",
    "FileNavigationScreen",
    "FindReplaceBar",
    "ResultMessage",
    "ResultTable",
    "SqlEditor",
    "SqlFileTree",
]
