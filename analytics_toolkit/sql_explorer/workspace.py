"""A complete, independently stateful SQL Explorer tab workspace."""

# ruff: noqa: FBT001, SLF001

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Button, Input, OptionList, Static

from .editor import SqlEditor
from .status import QuerySummaryBar
from .widgets import CommandInput, CompletionMenu, FindReplaceBar, ResultMessage, ResultTable

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from textual.app import ComposeResult
    from textual.widget import Widget

    from .completion import CompletionContext, CompletionCoordinator
    from .runtime import DatabaseSelection, ExplorerRunResult, ExplorerSession

WorkspaceQueryState = Literal["ready", "queued", "running", "cancelling"]


class SqlExplorerWorkspace(Vertical):
    """Own the widgets and mutable state that must not leak between tabs."""

    def __init__(
        self,
        tab_id: str,
        untitled_number: int,
        session: ExplorerSession,
    ) -> None:
        super().__init__(id=f"workspace-{tab_id}", classes="sql-workspace")
        self.tab_id = tab_id
        self.untitled_number = untitled_number
        self.session = session
        self.busy = False
        self.cancelling = False
        self.results_open = False
        self.query_state: WorkspaceQueryState = "ready"
        self.running_job_id: int | None = None
        self.operation_database: DatabaseSelection | None = None
        self.current_file: Path | None = None
        self.saved_text = ""
        self.completion: CompletionCoordinator | None = None
        self.completion_context: CompletionContext | None = None
        self.completion_candidates: tuple[str, ...] = ()
        self.completion_requested_text: str | None = None
        self.completion_cursor: tuple[int, int] | None = None
        self.completion_epoch = 0
        self.completion_loading_notice: str | None = None
        self.completion_allow_empty_columns = False
        self.create_table_draft: dict[str, Any] = {}
        self.last_focused: Widget | None = None
        self.closing = False
        self.exit_after_cancel = False
        self.pending_mount_action: Callable[[], None] | None = None
        self.last_run_result: ExplorerRunResult | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="query-pane"):
            yield SqlEditor(
                id="query-editor",
                show_line_numbers=True,
                soft_wrap=False,
            )
            yield FindReplaceBar(id="find-replace-bar")
            yield CompletionMenu(id="completion-menu", wrap=False)
            yield Static("SQL  Ln 1, Col 1", id="editor-status", markup=False)
        with Vertical(classes="result-pane"):
            with Horizontal(classes="results-header"):
                yield Static("Query output", id="result-title", markup=False)
                yield Button(
                    "\u00d7", id="close-results", classes="close-results", tooltip="Close results"
                )
            yield ResultTable(id="result-table", cursor_type="cell", zebra_stripes=True)
            yield ResultMessage("", id="result-message", markup=False)
        with Vertical(classes="command-panel"):
            yield QuerySummaryBar(id="query-summary")
            with Horizontal(id="command-row"):
                yield OptionList(id="command-completion", wrap=False)
                yield CommandInput(placeholder=": command", id="command-input")

    def on_mount(self) -> None:
        if self.tab_id != "1":
            app = cast("Any", self.app)
            app._finish_added_workspace(self.tab_id)
            action = self.pending_mount_action
            self.pending_mount_action = None
            if action is not None:
                action()

    @property
    def editor(self) -> SqlEditor:
        return self.query_one("#query-editor", SqlEditor)

    @property
    def command_input(self) -> Input:
        return self.query_one("#command-input", Input)

    @property
    def result_table(self) -> ResultTable:
        return self.query_one("#result-table", ResultTable)

    @property
    def result_message(self) -> ResultMessage:
        return self.query_one("#result-message", ResultMessage)

    @property
    def completion_menu(self) -> CompletionMenu:
        return self.query_one("#completion-menu", CompletionMenu)

    @property
    def find_bar(self) -> FindReplaceBar:
        return self.query_one("#find-replace-bar", FindReplaceBar)

    @property
    def is_dirty(self) -> bool:
        try:
            return self.editor.text != self.saved_text
        except NoMatches:
            return False

    @property
    def is_clean_untitled(self) -> bool:
        try:
            return self.current_file is None and not self.editor.text
        except NoMatches:
            return self.current_file is None and not self.saved_text

    @property
    def file_label(self) -> str:
        label = self.current_file.name if self.current_file is not None else self.untitled_label
        return f"{label}*" if self.is_dirty else label

    @property
    def untitled_label(self) -> str:
        return f"Untitled {self.untitled_number}"

    @property
    def tab_title(self) -> str:
        return f"[{self.session.database.connection_key}] {self.file_label}"

    def remember_focus(self, focused: Widget | None) -> None:
        if focused is not None and (focused is self or self in focused.ancestors):
            self.last_focused = focused

    def restore_focus(self) -> None:
        target = self.last_focused
        if target is not None and target.is_mounted and not target.disabled:
            target.focus()
        else:
            self.editor.focus()

    def set_visible(self, visible: bool) -> None:
        self.styles.display = "block" if visible else "none"

    def reset_query_state(self) -> None:
        self.busy = False
        self.cancelling = False
        self.query_state = "ready"
        self.running_job_id = None
        self.operation_database = None

    def contains(self, widget: Widget | None) -> bool:
        return widget is not None and (widget is self or self in widget.ancestors)


def workspace_for(node: Any) -> SqlExplorerWorkspace:
    """Return the workspace ancestor for an Explorer child widget."""
    if isinstance(node, SqlExplorerWorkspace):
        return node
    for ancestor in node.ancestors:
        if isinstance(ancestor, SqlExplorerWorkspace):
            return ancestor
    message = "SQL Explorer widget is not attached to a workspace."
    raise RuntimeError(message)


__all__ = ["SqlExplorerWorkspace", "WorkspaceQueryState", "workspace_for"]
