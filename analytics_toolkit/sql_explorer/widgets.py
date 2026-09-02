from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar, Tuple, cast

import pandas as pd
from rich.style import Style
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static, TextArea
from typing_extensions import override

if TYPE_CHECKING:
    from rich.text import Text
    from textual.app import ComposeResult

    from .app import SqlExplorerApp
    from .statements import ExplorerExecutionPlan

_MAX_CELL_LENGTH = 512
_MAX_CONFIRMATION_PREVIEW_LENGTH = 2_000
_INDENT = "    "
_SEARCH_MATCH_STYLE = Style(color="black", bgcolor="bright_yellow", bold=True)

SearchMatch = Tuple[Tuple[int, int], Tuple[int, int]]


class SqlEditor(TextArea):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "cursor_up", "Cursor up", show=False),
        Binding("down", "cursor_down", "Cursor down", show=False),
        Binding("home", "cursor_line_start", "Line start", show=False),
        Binding("end", "cursor_line_end", "Line end", show=False),
        Binding(
            "shift+home",
            "cursor_line_start(True)",
            "Select to line start",
            show=False,
        ),
        Binding(
            "shift+end",
            "cursor_line_end(True)",
            "Select to line end",
            show=False,
        ),
        Binding("ctrl+a", "select_all", "Select all", show=False),
        Binding("ctrl+x", "cut", "Cut", show=False),
        Binding("ctrl+v", "paste", "Paste", show=False),
        Binding("ctrl+z", "undo", "Undo", show=False),
        Binding("ctrl+y,ctrl+shift+z", "redo", "Redo", show=False),
        Binding("ctrl+home", "cursor_document_start", "Document start", show=False),
        Binding("ctrl+end", "cursor_document_end", "Document end", show=False),
    ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._search_pattern = ""
        self._search_matches: tuple[SearchMatch, ...] = ()
        self._search_index = -1
        super().__init__(*args, **kwargs)

    @property
    def search_match_count(self) -> int:
        return len(self._search_matches)

    @property
    def search_position(self) -> int | None:
        return self._search_index + 1 if self._search_index >= 0 else None

    def set_search_pattern(self, pattern: str) -> int:
        self._search_pattern = pattern
        self.refresh_search_matches()
        return self.search_match_count

    def refresh_search_matches(self) -> None:
        pattern = self._search_pattern
        matches: list[SearchMatch] = []
        if pattern:
            expression = re.compile(re.escape(pattern), flags=re.IGNORECASE)
            for row in range(self.document.line_count):
                matches.extend(
                    ((row, match.start()), (row, match.end()))
                    for match in expression.finditer(self.document[row])
                )
        self._search_matches = tuple(matches)
        selected = tuple(sorted(self.selection))
        self._search_index = next(
            (index for index, match in enumerate(self._search_matches) if match == selected),
            -1,
        )
        self.refresh()

    def select_next_search_match(self) -> int | None:
        if not self._search_matches:
            return None
        self._search_index = (self._search_index + 1) % len(self._search_matches)
        self._select_search_match(self._search_index)
        return self._search_index + 1

    def replace_current_search_match(self, replacement: str) -> bool:
        if not self._search_matches:
            return False
        index = max(self._search_index, 0)
        start, end = self._search_matches[index]
        result = self.replace(replacement, start, end, maintain_selection_offset=False)
        resume_at = result.end_location
        self.refresh_search_matches()
        if self._search_matches:
            self._search_index = next(
                (
                    match_index
                    for match_index, (match_start, _) in enumerate(self._search_matches)
                    if match_start >= resume_at
                ),
                0,
            )
            self._select_search_match(self._search_index)
        return True

    def replace_all_search_matches(self, replacement: str) -> int:
        if not self._search_pattern:
            return 0
        updated, count = re.subn(
            re.escape(self._search_pattern),
            lambda _: replacement,
            self.text,
            flags=re.IGNORECASE,
        )
        if count:
            self.text = updated
        self.refresh_search_matches()
        return count

    def clear_search(self) -> None:
        self._search_pattern = ""
        self.refresh_search_matches()

    def get_line(self, line_index: int) -> Text:
        line = super().get_line(line_index)
        for (start_row, start_column), (end_row, end_column) in self._search_matches:
            if start_row == line_index == end_row:
                line.stylize(_SEARCH_MATCH_STYLE, start_column, end_column)
        return line

    def _select_search_match(self, index: int) -> None:
        start, end = self._search_matches[index]
        self.move_cursor(start)
        self.move_cursor(end, select=True, center=True)

    def action_cut(self) -> None:
        selected = self.selected_text
        if not selected:
            return
        cast("SqlExplorerApp", self.app).copy_to_explorer_clipboard(selected)
        start, end = self.selection
        self.replace("", start, end, maintain_selection_offset=False)

    def action_paste(self) -> None:
        value = cast("SqlExplorerApp", self.app).paste_from_explorer_clipboard()
        if value:
            start, end = self.selection
            self.replace(value, start, end, maintain_selection_offset=False)

    def action_cursor_document_start(self) -> None:
        self.cursor_location = (0, 0)

    def action_cursor_document_end(self) -> None:
        last_row = self.document.line_count - 1
        self.cursor_location = (last_row, len(self.document[last_row]))

    @override
    def action_cursor_up(self, select: bool = False) -> None:
        if not select and self.cursor_location[0] == 0:
            cast("SqlExplorerApp", self.app).action_focus_previous_pane()
            return
        super().action_cursor_up(select=select)

    @override
    def action_cursor_down(self, select: bool = False) -> None:
        if not select and self.cursor_location[0] == self.document.line_count - 1:
            cast("SqlExplorerApp", self.app).action_focus_next_pane()
            return
        super().action_cursor_down(select=select)

    @override
    def action_cursor_line_start(self, select: bool = False) -> None:
        row, _ = self.cursor_location
        self.move_cursor((row, 0), select=select)

    def action_indent(self) -> None:
        start, end = self.selection
        result = self.replace(_INDENT, start, end)
        self.cursor_location = result.end_location

    def action_unindent(self) -> None:
        row, column = self.cursor_location
        line = self.document[row]
        removable = min(len(line) - len(line.lstrip(" ")), len(_INDENT), column)
        if removable:
            self.replace("", (row, column - removable), (row, column))
            self.cursor_location = (row, column - removable)


class ResultTable(DataTable[Any]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "cursor_up", "Cursor up", show=False),
        Binding("down", "cursor_down", "Cursor down", show=False),
        Binding("delete", "close_results", "Close results", show=False),
    ]

    def action_cursor_up(self) -> None:
        if self.row_count == 0 or self.cursor_row == 0:
            cast("SqlExplorerApp", self.app).action_focus_previous_pane()
            return
        super().action_cursor_up()

    def action_cursor_down(self) -> None:
        if self.row_count == 0 or self.cursor_row >= self.row_count - 1:
            cast("SqlExplorerApp", self.app).action_focus_next_pane()
            return
        super().action_cursor_down()

    def action_close_results(self) -> None:
        cast("SqlExplorerApp", self.app).close_results()


class ResultMessage(Static, can_focus=True):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "focus_previous_pane", "Previous pane", show=False),
        Binding("down", "focus_next_pane", "Next pane", show=False),
        Binding("delete", "close_results", "Close results", show=False),
    ]

    def action_focus_previous_pane(self) -> None:
        cast("SqlExplorerApp", self.app).action_focus_previous_pane()

    def action_focus_next_pane(self) -> None:
        cast("SqlExplorerApp", self.app).action_focus_next_pane()

    def action_close_results(self) -> None:
        cast("SqlExplorerApp", self.app).close_results()


class CommandInput(Input):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "focus_previous_pane", "Previous pane", show=False),
        Binding("down", "focus_next_pane", "Next pane", show=False),
    ]

    def action_focus_previous_pane(self) -> None:
        cast("SqlExplorerApp", self.app).action_focus_previous_pane()

    def action_focus_next_pane(self) -> None:
        cast("SqlExplorerApp", self.app).action_focus_next_pane()


class FindReplaceBar(Vertical):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close find", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Horizontal(id="find-row"):
            yield Input(placeholder="Find", id="find-pattern")
            yield Button("Next", id="find-next")
        with Horizontal(id="replace-row"):
            yield Input(placeholder="Replace", id="replace-pattern")
            yield Button("Replace", id="replace-current")
            yield Button("Replace All", id="replace-all")

    def open(self) -> None:
        editor = self.app.query_one("#query-editor", SqlEditor)
        search_input = self.query_one("#find-pattern", Input)
        selected = editor.selected_text
        if selected and "\n" not in selected:
            search_input.value = selected
        self.styles.display = "block"
        editor.set_search_pattern(search_input.value)
        search_input.focus()
        self._update_notice()

    def action_close(self) -> None:
        self.styles.display = "none"
        editor = self.app.query_one("#query-editor", SqlEditor)
        editor.clear_search()
        editor.focus()

    def find_next(self) -> None:
        position = self.app.query_one("#query-editor", SqlEditor).select_next_search_match()
        self._update_notice(position)

    def replace_current(self) -> None:
        editor = self.app.query_one("#query-editor", SqlEditor)
        replacement = self.query_one("#replace-pattern", Input).value
        editor.replace_current_search_match(replacement)
        self._update_notice(editor.search_position)

    def replace_all(self) -> None:
        editor = self.app.query_one("#query-editor", SqlEditor)
        replacement = self.query_one("#replace-pattern", Input).value
        count = editor.replace_all_search_matches(replacement)
        cast("SqlExplorerApp", self.app).show_notice(f"Replaced {count} occurrence(s).")

    def _update_notice(self, position: int | None = None) -> None:
        editor = self.app.query_one("#query-editor", SqlEditor)
        pattern = self.query_one("#find-pattern", Input).value
        if not pattern:
            message = "Enter text to find."
        elif not editor.search_match_count:
            message = "No matches found."
        elif position is None:
            message = f"Found {editor.search_match_count} occurrence(s)."
        else:
            message = f"Match {position} of {editor.search_match_count}."
        cast("SqlExplorerApp", self.app).show_notice(message)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "find-pattern":
            self.find_next()
        elif event.input.id == "replace-pattern":
            self.replace_current()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "find-pattern":
            return
        self.app.query_one("#query-editor", SqlEditor).set_search_pattern(event.value)
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
        preview = "\n\n".join(self.plan.statements)
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-execute")


def _format_cell(value: object) -> str:
    if value is None:
        return "NULL"
    try:
        if bool(pd.isna(value)):
            return "NULL"
    except (TypeError, ValueError):
        pass
    rendered = str(value).replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")
    if len(rendered) > _MAX_CELL_LENGTH:
        return rendered[: _MAX_CELL_LENGTH - 1] + "…"
    return rendered


__all__ = [
    "CommandInput",
    "ConfirmMutationScreen",
    "FindReplaceBar",
    "ResultMessage",
    "ResultTable",
    "SqlEditor",
]
