from __future__ import annotations

import shlex
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pyperclip
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Button, Input, OptionList, Static, TextArea

from .clipboard import TerminalClipboard
from .commands import HELP_TEXT, SqlExplorerCursorCommandsMixin
from .completion import CompletionContext, CompletionCoordinator, CompletionCoordinatorPool
from .completion_commands import SqlExplorerCompletionCommandsMixin
from .editor import SqlEditor
from .errors import SqlExplorerConfigurationError
from .exports import SqlExplorerExportCommandsMixin
from .file_commands import SqlExplorerFileCommandsMixin
from .filetree import read_sql_file
from .inputs import EditableInput
from .picker import DatabasePickerApp
from .query_commands import SqlExplorerQueryCommandsMixin
from .scheduling import ExplorerQueryScheduler
from .status import QuerySummaryBar, query_summary_for
from .styles import APP_CSS, explorer_css_variables
from .tabs import (
    NewTabButton,
    SqlExplorerTabCommandsMixin,
    TabCloseButton,
    TabSelectButton,
    WorkspaceTab,
    WorkspaceTabStrip,
)
from .terminal_keys import control_compatible_key
from .widgets import (
    CommandInput,
    CompletionMenu,
    ConfirmMutationScreen,
    FileNavigationScreen,
    FindReplaceBar,
    ResultMessage,
    ResultTable,
    _format_cell,
)
from .workspace import SqlExplorerWorkspace, workspace_for

if TYPE_CHECKING:
    import pandas as pd
    from textual.widget import Widget

    from .runtime import ExplorerSession


def _remove_dynamic_binding(bindings: Any, key: str) -> None:
    storage = getattr(bindings, "keys", None)
    if storage is None:
        storage = bindings.key_to_bindings
    storage.pop(key, None)


class SqlExplorerApp(
    SqlExplorerCompletionCommandsMixin,
    SqlExplorerQueryCommandsMixin,
    SqlExplorerTabCommandsMixin,
    SqlExplorerCursorCommandsMixin,
    SqlExplorerFileCommandsMixin,
    SqlExplorerExportCommandsMixin,
    App[None],
):
    TITLE = "analytics-toolkit SQL explorer"
    CSS = APP_CSS
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f5", "run_query", "Run", priority=True),
        Binding("ctrl+o", "open_navigation", "Open SQL file", priority=True),
        Binding("ctrl+s", "save_file", "Save SQL file", show=False, priority=True),
        Binding("ctrl+n", "new_sql_file", "New SQL file", show=False, priority=True),
        Binding("ctrl+t", "new_tab", "New tab", show=False, priority=True),
        Binding("ctrl+w", "close_tab", "Close tab", show=False, priority=True),
        Binding("ctrl+tab", "next_tab", "Next tab", show=False, priority=True),
        Binding(
            "ctrl+shift+tab",
            "previous_tab",
            "Previous tab",
            show=False,
            priority=True,
        ),
        Binding(
            "meta+o,super+o",
            "open_navigation",
            "Open SQL file",
            show=False,
            priority=True,
        ),
        Binding(
            "kp_enter,hyper+enter,meta+enter,super+enter",
            "run_query",
            "Run",
            show=False,
            priority=True,
        ),
        Binding("tab", "plain_tab", "Indent", show=False, priority=True),
        Binding("shift+tab", "plain_shift_tab", "Unindent", show=False, priority=True),
        Binding("ctrl+c", "copy_focused", "Copy", show=False, priority=True),
        Binding("ctrl+f", "open_find", "Find", show=False, priority=True),
        Binding("escape", "escape", "Toggle editor/command", show=False),
    ]

    def __init__(self, session: ExplorerSession) -> None:
        super().__init__()
        self._workspace_sequence = 1
        self._untitled_sequence = 1
        self._active_tab_id = "1"
        initial = SqlExplorerWorkspace("1", 1, session)
        self._workspaces: dict[str, SqlExplorerWorkspace] = {"1": initial}
        self._tab_order: list[str] = ["1"]
        self._query_scheduler = ExplorerQueryScheduler()
        self._completion_pool = CompletionCoordinatorPool()
        self._exit_requested = False
        self._exit_dirty_tabs: list[str] = []
        self._primary_binding: str | None = None
        self._clipboard = ""
        self._terminal_clipboard = TerminalClipboard()
        self._find_navigation_bound = False

    def get_css_variables(self) -> dict[str, str]:
        return explorer_css_variables()

    @property
    def active_workspace(self) -> SqlExplorerWorkspace:
        return self._workspaces[self._active_tab_id]

    @property
    def session(self) -> ExplorerSession:
        return self.active_workspace.session

    @property
    def busy(self) -> bool:
        return self.active_workspace.busy

    @busy.setter
    def busy(self, value: bool) -> None:
        self.active_workspace.busy = bool(value)

    @property
    def cancelling(self) -> bool:
        return self.active_workspace.cancelling

    @cancelling.setter
    def cancelling(self, value: bool) -> None:
        self.active_workspace.cancelling = bool(value)

    @property
    def results_open(self) -> bool:
        return self.active_workspace.results_open

    @results_open.setter
    def results_open(self, value: bool) -> None:
        self.active_workspace.results_open = bool(value)

    @property
    def _current_file(self) -> Path | None:
        return self.active_workspace.current_file

    @_current_file.setter
    def _current_file(self, value: Path | None) -> None:
        self.active_workspace.current_file = value

    @property
    def _saved_text(self) -> str:
        return self.active_workspace.saved_text

    @_saved_text.setter
    def _saved_text(self, value: str) -> None:
        self.active_workspace.saved_text = value

    @property
    def _completion(self) -> CompletionCoordinator | None:
        return self.active_workspace.completion

    @_completion.setter
    def _completion(self, value: CompletionCoordinator | None) -> None:
        self.active_workspace.completion = value

    @property
    def _completion_context(self) -> CompletionContext | None:
        return self.active_workspace.completion_context

    @_completion_context.setter
    def _completion_context(self, value: CompletionContext | None) -> None:
        self.active_workspace.completion_context = value

    @property
    def _exit_after_cancel(self) -> bool:
        return self.active_workspace.exit_after_cancel

    @_exit_after_cancel.setter
    def _exit_after_cancel(self, value: bool) -> None:
        self.active_workspace.exit_after_cancel = bool(value)

    async def on_event(self, event: events.Event) -> None:
        if isinstance(event, events.Key) and not event.is_forwarded:
            compatible = control_compatible_key(event.key)
            if compatible != event.key:
                event = events.Key(compatible, None)
        await super().on_event(event)

    def compose(self) -> ComposeResult:
        with WorkspaceTabStrip(id="tab-strip"):
            yield WorkspaceTab("1", self.active_workspace.tab_title)
            yield NewTabButton()
        with Vertical(id="workspace-stack"):
            yield self.active_workspace

    def on_mount(self) -> None:
        workspace = self.active_workspace
        workspace.result_message.styles.display = "none"
        workspace.completion_menu.styles.display = "none"
        self._install_primary_binding(self.session.settings.run_binding)
        self._start_completion_coordinator(workspace)
        self._refresh_tab(workspace)
        self.query_one("#tab-1", WorkspaceTab).set_active(True)
        self._refresh_close_buttons()
        self._update_status(workspace)
        self._update_editor_status(workspace)
        if self.session.settings_warning:
            self._set_notice(self.session.settings_warning, workspace)
        workspace.editor.focus()
        self.set_interval(0.1, self._update_all_statuses)

    def on_unmount(self) -> None:
        self._completion_pool.stop()

    def action_focus_next_pane(self) -> None:
        self._focus_relative(1)

    def action_focus_previous_pane(self) -> None:
        self._focus_relative(-1)

    def action_escape(self) -> None:
        # Modal screens own Escape; the app-level priority binding must not
        # dismiss or navigate behind them.
        if len(self.screen_stack) > 1:
            return
        workspace = self.active_workspace
        bar = workspace.find_bar
        if bar.styles.display != "none":
            bar.action_close()
            return
        menu = workspace.completion_menu
        if menu.is_open:
            menu.action_close()
            return
        editor = workspace.editor
        if editor.cursor_count > 1:
            editor.collapse_to_active()
            return
        if isinstance(self.focused, (SqlEditor, ResultTable, ResultMessage)):
            workspace.command_input.focus()
            return
        editor.focus()

    def action_plain_tab(self) -> None:
        if isinstance(self.screen, FileNavigationScreen):
            self.screen.action_complete_path()
            return
        menu = self.active_workspace.completion_menu
        if menu.is_open:
            self._accept_completion()
            return
        focused = self.focused
        if isinstance(focused, SqlEditor) and not self._request_completion():
            focused.action_indent()

    def action_plain_shift_tab(self) -> None:
        if isinstance(self.screen, FileNavigationScreen):
            self.screen.action_previous_match()
            return
        focused = self.focused
        if isinstance(focused, SqlEditor):
            focused.action_unindent()

    def action_copy_focused(self) -> None:
        focused = self.focused
        if isinstance(focused, EditableInput):
            if focused.copy_selection():
                self._set_notice("Copied selection.")
        elif isinstance(focused, SqlEditor):
            self.copy_to_explorer_clipboard(focused.command_copy_text())
            has_selection = any(not item.is_empty for item in focused.cursor_selections)
            self._set_notice("Copied editor selection." if has_selection else "Copied editor.")
        elif isinstance(focused, ResultTable):
            value = focused.copy_text()
            if value:
                self.copy_to_explorer_clipboard(value)
                self._set_notice("Copied selection.")

    def action_open_find(self) -> None:
        workspace = self.active_workspace
        workspace.editor.collapse_to_active()
        workspace.find_bar.open()

    def action_find_previous_control(self) -> None:
        self._focus_find_control(-1)

    def action_find_next_control(self) -> None:
        self._focus_find_control(1)

    def action_open_navigation(self) -> None:
        if len(self.screen_stack) > 1:
            return
        tab_id = self._active_tab_id
        self.push_screen(
            FileNavigationScreen(Path.cwd()),
            lambda path: self._navigation_closed(path, tab_id),
        )

    def enable_find_navigation(self) -> None:
        if self._find_navigation_bound:
            return
        self._bindings.bind(
            "up",
            "find_previous_control",
            "Find/Replace previous control",
            show=False,
            priority=True,
        )
        self._bindings.bind(
            "down",
            "find_next_control",
            "Find/Replace next control",
            show=False,
            priority=True,
        )
        self._find_navigation_bound = True
        self.refresh_bindings()

    def disable_find_navigation(self) -> None:
        if not self._find_navigation_bound:
            return
        _remove_dynamic_binding(self._bindings, "up")
        _remove_dynamic_binding(self._bindings, "down")
        self._find_navigation_bound = False
        self.refresh_bindings()

    def _focus_find_control(self, direction: int) -> None:
        if len(self.screen_stack) != 1:
            return
        bar = self.active_workspace.find_bar
        if bar.is_open:
            bar.focus_relative(direction)

    def _focus_relative(self, direction: int) -> None:
        workspace = self.active_workspace
        panes: list[Widget] = [workspace.editor]
        if workspace.results_open:
            table = workspace.result_table
            message = workspace.result_message
            panes.append(message if message.styles.display != "none" else table)
        panes.append(workspace.command_input)
        focused = self.focused
        try:
            index = panes.index(focused) if focused is not None else -1
        except ValueError:
            index = -1 if direction > 0 else 0
        panes[(index + direction) % len(panes)].focus()

    def show_dataframe(
        self,
        dataframe: pd.DataFrame,
        workspace: SqlExplorerWorkspace | None = None,
    ) -> None:
        workspace = workspace or self.active_workspace
        table = workspace.result_table
        table.clear_rectangular_selection()
        table.clear(columns=True)
        for index, column in enumerate(dataframe.columns):
            table.add_column(Text(str(column)), key=f"column-{index}")
        for row_number, row in enumerate(dataframe.itertuples(index=False, name=None), 1):
            table.add_row(
                *(Text(_format_cell(value)) for value in row),
                label=str(row_number),
            )
        workspace.result_message.styles.display = "none"
        table.styles.display = "block"
        self._open_results(workspace)

    def show_message(
        self,
        message: str,
        workspace: SqlExplorerWorkspace | None = None,
    ) -> None:
        workspace = workspace or self.active_workspace
        table = workspace.result_table
        table.styles.display = "none"
        result_message = workspace.result_message
        result_message.update(message)
        result_message.styles.display = "block"
        self._open_results(workspace)

    def show_error(
        self,
        exc: Exception,
        workspace: SqlExplorerWorkspace | None = None,
    ) -> None:
        workspace = workspace or self.active_workspace
        message = f"{type(exc).__name__}: {exc}"
        query = getattr(workspace.session, "last_query", None)
        notice = (
            "SQL explorer query was cancelled."
            if query is not None and query.state == "cancelled"
            else "SQL explorer operation failed."
        )
        self._set_notice(notice, workspace)
        self.show_message(message, workspace)
        self._update_status(workspace)

    def show_notice(
        self,
        message: str,
        workspace: SqlExplorerWorkspace | None = None,
    ) -> None:
        self._set_notice(message, workspace)

    def _open_results(self, workspace: SqlExplorerWorkspace | None = None) -> None:
        workspace = workspace or self.active_workspace
        workspace.query_one(".result-pane", Vertical).styles.display = "block"
        workspace.results_open = True

    def close_results(
        self,
        *,
        focus_editor: bool = True,
        workspace: SqlExplorerWorkspace | None = None,
    ) -> None:
        workspace = workspace or self.active_workspace
        pane = workspace.query_one(".result-pane", Vertical)
        pane.styles.display = "none"
        table = workspace.result_table
        table.clear_rectangular_selection()
        table.clear(columns=True)
        workspace.result_message.update("")
        workspace.results_open = False
        if focus_editor:
            workspace.editor.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "command-input":
            return
        command_text = event.value.strip()
        event.input.value = ""
        if not command_text:
            return
        try:
            command = command_text[1:] if command_text.startswith(":") else command_text
            parts = shlex.split(command)
        except ValueError as exc:
            self.show_error(SqlExplorerConfigurationError(str(exc)))
            return
        workspace = workspace_for(event.input)
        self._activate_tab(workspace.tab_id)
        self._handle_command(parts)
        if len(self.screen_stack) == 1:
            workspace.command_input.focus()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if isinstance(event.text_area, SqlEditor):
            workspace = workspace_for(event.text_area)
            event.text_area.refresh_search_matches()
            self._refresh_tab(workspace)
            self._update_editor_status(workspace)
            if len(self.screen_stack) > 1:
                return
            self._refresh_open_completion(workspace)
            self._update_status(workspace)

    def on_text_area_selection_changed(self, event: TextArea.SelectionChanged) -> None:
        if isinstance(event.text_area, SqlEditor):
            self._update_editor_status(workspace_for(event.text_area))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if isinstance(event.option_list, CompletionMenu):
            event.stop()
            self._accept_completion(workspace_for(event.option_list))

    def load_sql_file(
        self,
        path: str | Path,
        source_workspace: SqlExplorerWorkspace | None = None,
    ) -> None:
        resolved = Path(path).resolve()
        for workspace in self._workspaces.values():
            if workspace.current_file == resolved:
                self._activate_tab(workspace.tab_id)
                return
        try:
            text = read_sql_file(resolved)
        except (OSError, UnicodeError) as exc:
            self.show_message(f"{type(exc).__name__}: {exc}")
            return
        source = source_workspace or self.active_workspace
        workspace = source if source.is_clean_untitled else self._add_workspace()
        if workspace.is_mounted:
            self._populate_file(workspace, resolved, text)
        else:
            workspace.pending_mount_action = lambda: self._populate_file(
                workspace,
                resolved,
                text,
            )

    def _load_sql_file_now(
        self,
        path: Path,
        workspace: SqlExplorerWorkspace | None = None,
    ) -> None:
        self.load_sql_file(path, workspace)

    def _populate_file(
        self,
        workspace: SqlExplorerWorkspace,
        path: Path,
        text: str,
    ) -> None:
        editor = workspace.editor
        editor.text = text
        editor.cursor_location = (0, 0)
        workspace.current_file = path
        workspace.saved_text = text
        self._activate_tab(workspace.tab_id)
        self._set_notice(f"Loaded {path}", workspace)
        self._refresh_tab(workspace)
        self._update_status(workspace)
        self.call_after_refresh(self._update_status, workspace)
        editor.focus()

    def _navigation_closed(self, path: Path | None, tab_id: str | None = None) -> None:
        if path is not None:
            workspace = self._workspaces.get(tab_id or self._active_tab_id)
            self.load_sql_file(path, workspace)

    def _handle_command(self, parts: list[str]) -> None:
        if not parts:
            return
        command, *arguments = [part.strip() for part in parts]
        command = command.lower()
        handlers = {
            "cancel": self._command_cancel,
            "clear": self._command_clear,
            "confirm": self._command_confirmation,
            "db": self._command_database,
            "exit": self._command_exit,
            "format": self._command_format,
            "help": self._command_help,
            "mode": self._command_mode,
            "mv": self._command_move,
            "mvs": self._command_move_select,
            "open": self._command_open,
            "cp": self._command_copy,
            "pst": self._command_paste,
            "quit": self._command_exit,
            "run": self._command_run,
            "save": self._command_save,
            "shortcut": self._command_shortcut,
            "to_csv": self._command_to_csv,
            "to_excel": self._command_to_excel,
        }
        handler = handlers.get(command)
        if handler is None:
            self.show_error(SqlExplorerConfigurationError(f"Unknown command: {command}"))
            return
        handler(arguments)

    def _command_run(self, arguments: list[str]) -> None:
        if arguments:
            self.show_error(SqlExplorerConfigurationError("Usage: run"))
            return
        self.action_run_query()

    def _command_open(self, arguments: list[str]) -> None:
        if arguments:
            self.show_error(SqlExplorerConfigurationError("Usage: open"))
            return
        self.action_open_navigation()

    def _command_mode(self, arguments: list[str]) -> None:
        if not arguments or arguments == ["exploratory"]:
            self._set_notice("Current mode: exploratory.")
            return
        if arguments == ["navigation"]:
            self.action_open_navigation()
            return
        self.show_error(
            SqlExplorerConfigurationError("Available modes are exploratory and navigation.")
        )

    def _command_help(self, arguments: list[str]) -> None:
        if arguments:
            self.show_error(SqlExplorerConfigurationError("Usage: help"))
            return
        self.show_message(HELP_TEXT)

    def _command_exit(self, arguments: list[str]) -> None:
        if arguments:
            self.show_error(SqlExplorerConfigurationError("Usage: exit"))
        else:
            self._request_exit()

    def _command_cancel(self, arguments: list[str]) -> None:
        if arguments:
            self.show_error(SqlExplorerConfigurationError("Usage: cancel"))
            return
        self._request_cancel(workspace=self.active_workspace, exit_after=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if isinstance(event.button, TabSelectButton):
            self._activate_tab(event.button.tab_id)
        elif isinstance(event.button, TabCloseButton):
            self._request_close_tab(event.button.tab_id)
        elif isinstance(event.button, NewTabButton):
            self.action_new_tab()
        elif event.button.has_class("interrupt"):
            self._request_cancel(
                workspace=workspace_for(event.button),
                exit_after=False,
            )

    def _command_database(self, arguments: list[str]) -> None:
        if len(arguments) != 1:
            self.show_error(SqlExplorerConfigurationError("Usage: db DB_KEY"))
            return
        workspace = self.active_workspace
        if workspace.busy:
            self._set_notice(
                "Wait for the current SQL operation before switching databases.",
                workspace,
            )
            return
        previous_key = workspace.session.database.connection_key
        try:
            database = workspace.session.switch_database(arguments[0])
        except Exception as exc:  # noqa: BLE001 -- validation errors are rendered in the TUI.
            self.show_error(exc, workspace)
            return
        self._completion_pool.release(workspace.tab_id, previous_key)
        workspace.completion = None
        self.close_results(focus_editor=False, workspace=workspace)
        self._start_completion_coordinator(workspace)
        self._refresh_tab(workspace)
        self._update_status(workspace)
        self._set_notice(
            f"Switched to {database.connection_key} ({database.backend}).",
            workspace,
        )

    def _command_shortcut(self, arguments: list[str]) -> None:
        if len(arguments) != 1:
            self.show_error(SqlExplorerConfigurationError("Usage: shortcut KEY|reset"))
            return
        try:
            settings = self.session.set_run_binding(arguments[0])
        except Exception as exc:  # noqa: BLE001 -- settings errors are rendered in the TUI.
            self.show_error(exc)
            return
        for workspace in self._workspaces.values():
            workspace.session.settings = settings
        self._install_primary_binding(settings.run_binding)
        self._update_all_statuses()
        self._set_notice(f"Primary run shortcut: {settings.run_binding}.")

    def _command_confirmation(self, arguments: list[str]) -> None:
        if len(arguments) != 1 or arguments[0].lower() not in {"on", "off", "toggle"}:
            self.show_error(SqlExplorerConfigurationError("Usage: confirm on|off|toggle"))
            return
        value = arguments[0].lower()
        enabled = (
            not self.session.settings.confirm_mutations if value == "toggle" else value == "on"
        )
        try:
            settings = self.session.set_confirmation(enabled=enabled)
        except OSError as exc:
            self.show_error(exc)
            return
        for workspace in self._workspaces.values():
            workspace.session.settings = settings
        self._update_all_statuses()
        state = "on" if settings.confirm_mutations else "off"
        self._set_notice(f"Mutation confirmation is {state}.")

    def _command_clear(self, arguments: list[str]) -> None:
        if len(arguments) != 1 or arguments[0].lower() not in {"query", "results", "all"}:
            self.show_error(SqlExplorerConfigurationError("Usage: clear query|results|all"))
            return
        target = arguments[0].lower()
        workspace = self.active_workspace
        if target in {"query", "all"}:
            editor = workspace.editor
            editor.text = ""
            editor.collapse_to_active()
        if target in {"results", "all"}:
            self.close_results(workspace=workspace)
        self._set_notice(f"Cleared {target}.", workspace)

    def _install_primary_binding(self, binding: str) -> None:
        if self._primary_binding and self._primary_binding != "f5":
            _remove_dynamic_binding(self._bindings, self._primary_binding)
        if binding != "f5":
            self._bindings.bind(
                binding,
                "run_query",
                "Run",
                show=False,
                priority=True,
            )
        self._primary_binding = binding
        self.refresh_bindings()

    def copy_to_explorer_clipboard(self, value: str) -> None:
        self._terminal_clipboard.copy(value)
        with suppress(pyperclip.PyperclipException):
            pyperclip.copy(value)
        self._clipboard = value

    def paste_from_explorer_clipboard(self) -> str:
        try:
            value = pyperclip.paste()
        except pyperclip.PyperclipException:
            return self._clipboard
        return value or self._clipboard

    def _update_all_statuses(self) -> None:
        for workspace in tuple(self._workspaces.values()):
            self._update_status(workspace)

    def _update_status(self, workspace: SqlExplorerWorkspace | None = None) -> None:
        if not self.screen_stack:
            return
        workspace = workspace or self.active_workspace
        try:
            summary = workspace.query_one(QuerySummaryBar)
            interrupt = workspace.query_one(".interrupt", Button)
        except NoMatches:
            return
        summary.update_presentation(query_summary_for(workspace))
        interrupt.disabled = not workspace.busy or workspace.cancelling
        self._refresh_tab(workspace)

    def _update_editor_status(self, workspace: SqlExplorerWorkspace | None = None) -> None:
        if not self.screen_stack:
            return
        workspace = workspace or self.active_workspace
        try:
            row, column = workspace.editor.cursor_location
            workspace.query_one("#editor-status", Static).update(
                f"SQL  Ln {row + 1}, Col {column + 1}"
            )
        except NoMatches:
            return

    def _set_notice(
        self,
        message: str | None,
        workspace: SqlExplorerWorkspace | None = None,
    ) -> None:
        if self.screen_stack:
            workspace = workspace or self.active_workspace
            with suppress(NoMatches):
                workspace.query_one("#notice", Static).update(message or "")


__all__ = [
    "CommandInput",
    "ConfirmMutationScreen",
    "DatabasePickerApp",
    "FindReplaceBar",
    "ResultMessage",
    "ResultTable",
    "SqlEditor",
    "SqlExplorerApp",
]
