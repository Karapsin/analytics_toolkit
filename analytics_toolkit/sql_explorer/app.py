from __future__ import annotations

import shlex
from contextlib import suppress
from typing import TYPE_CHECKING, ClassVar

import pyperclip
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Input, Static, TextArea

from .errors import SqlExplorerConfigurationError
from .picker import DatabasePickerApp
from .styles import APP_CSS
from .widgets import (
    CommandInput,
    ConfirmMutationScreen,
    FindReplaceBar,
    ResultMessage,
    ResultTable,
    SqlEditor,
    _format_cell,
)

if TYPE_CHECKING:
    import pandas as pd
    from textual.widget import Widget

    from .runtime import ExplorerCancelResult, ExplorerRunResult, ExplorerSession
    from .statements import ExplorerExecutionPlan

_HELP_TEXT = """Commands
  run                         execute the editor
  cancel                      cancel the active explorer query
  mode [exploratory]          show or select the current mode
  db DB_KEY                   switch the configured connection
  shortcut KEY|reset          change the primary run shortcut
  confirm on|off|toggle       control mutation confirmation
  clear query|results|all     clear workspace content
  help                        show this help
  exit | quit                 close the explorer

Keys
  Alt+Tab                     cycle panes
  Alt+Shift+Tab               cycle panes in reverse
  Up / Down                   cross pane boundaries
  Ctrl+Enter                  default run shortcut
  Fn+Enter                    run when reported as keypad Enter
  Cmd+Enter                   run when forwarded by a macOS terminal
  F5                          permanent run fallback
  Ctrl+F                      find and replace in the editor
  Delete                      close a focused result/error pane
"""


class SqlExplorerApp(App[None]):
    TITLE = "analytics-toolkit SQL explorer"
    CSS = APP_CSS
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f5", "run_query", "Run", priority=True),
        Binding(
            "kp_enter,hyper+enter,meta+enter,super+enter",
            "run_query",
            "Run",
            show=False,
            priority=True,
        ),
        Binding(
            "alt+tab",
            "focus_next_pane",
            "Next pane",
            show=False,
            priority=True,
        ),
        Binding(
            "alt+shift+tab",
            "focus_previous_pane",
            "Previous pane",
            show=False,
            priority=True,
        ),
        Binding("tab", "plain_tab", "Indent", show=False, priority=True),
        Binding("shift+tab", "plain_shift_tab", "Unindent", show=False, priority=True),
        Binding("ctrl+c", "copy_focused", "Copy", show=False, priority=True),
        Binding("ctrl+f", "open_find", "Find", show=False, priority=True),
    ]

    def __init__(self, session: ExplorerSession) -> None:
        super().__init__()
        self.session = session
        self.busy = False
        self.cancelling = False
        self.results_open = False
        self._exit_after_cancel = False
        self._primary_binding: str | None = None
        self._clipboard = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="workspace"):
            with Vertical(id="query-pane"):
                yield FindReplaceBar(id="find-replace-bar")
                yield SqlEditor(id="query-editor", show_line_numbers=True)
            with Vertical(id="result-pane"):
                yield ResultTable(id="result-table", cursor_type="cell", zebra_stripes=True)
                yield ResultMessage("", id="result-message", markup=False)
        with Vertical(id="command-panel"):
            yield Static("", id="session-status", markup=False)
            yield Static("", id="notice", markup=False)
            yield CommandInput(placeholder=": command", id="command-input")

    def on_mount(self) -> None:
        self.query_one("#result-message", ResultMessage).styles.display = "none"
        self._install_primary_binding(self.session.settings.run_binding)
        self._update_status()
        if self.session.settings_warning:
            self._set_notice(self.session.settings_warning)
        self.query_one("#query-editor", SqlEditor).focus()

    def action_run_query(self) -> None:
        if self.busy:
            self._set_notice("A SQL operation is already running.")
            return
        editor = self.query_one("#query-editor", SqlEditor)
        try:
            plan = self.session.plan(editor.text)
        except Exception as exc:  # noqa: BLE001 -- errors are rendered in the TUI.
            self.show_error(exc)
            return
        if plan.requires_confirmation and self.session.settings.confirm_mutations:
            self.push_screen(
                ConfirmMutationScreen(
                    plan,
                    db_key=self.session.database.connection_key,
                    backend=self.session.database.backend,
                ),
                lambda confirmed: self._start_execution(plan) if confirmed else None,
            )
            return
        self._start_execution(plan)

    def action_focus_next_pane(self) -> None:
        self._focus_relative(1)

    def action_focus_previous_pane(self) -> None:
        self._focus_relative(-1)

    def action_plain_tab(self) -> None:
        focused = self.focused
        if isinstance(focused, SqlEditor):
            focused.action_indent()

    def action_plain_shift_tab(self) -> None:
        focused = self.focused
        if isinstance(focused, SqlEditor):
            focused.action_unindent()

    def action_copy_focused(self) -> None:
        focused = self.focused
        if isinstance(focused, SqlEditor) and focused.selected_text:
            self.copy_to_explorer_clipboard(focused.selected_text)
            self._set_notice("Copied selection.")

    def action_open_find(self) -> None:
        self.query_one(FindReplaceBar).open()

    def _focus_relative(self, direction: int) -> None:
        panes: list[Widget] = [self.query_one("#query-editor", SqlEditor)]
        if self.results_open:
            table = self.query_one("#result-table", ResultTable)
            message = self.query_one("#result-message", ResultMessage)
            panes.append(message if message.styles.display != "none" else table)
        panes.append(self.query_one("#command-input", Input))
        focused = self.focused
        try:
            index = panes.index(focused) if focused is not None else -1
        except ValueError:
            index = -1 if direction > 0 else 0
        panes[(index + direction) % len(panes)].focus()

    def _start_execution(self, plan: ExplorerExecutionPlan) -> None:
        self.close_results(focus_editor=False)
        self.busy = True
        self._set_notice(f"Running via sql.{plan.route.value}...")
        self._update_status()
        self._execute_in_worker(plan)

    @work(thread=True, group="sql-explorer", exclusive=True, exit_on_error=False)
    def _execute_in_worker(self, plan: ExplorerExecutionPlan) -> None:
        try:
            result = self.session.execute(plan)
        except Exception as exc:  # noqa: BLE001 -- worker failures are rendered in the TUI.
            self.call_from_thread(self._finish_error, exc)
        else:
            self.call_from_thread(self._finish_result, result)

    @work(thread=True, group="sql-explorer-cancel", exclusive=True, exit_on_error=False)
    def _cancel_in_worker(self) -> None:
        try:
            result = self.session.cancel_active()
        except Exception as exc:  # noqa: BLE001 -- errors are rendered in the TUI.
            self.call_from_thread(self._finish_cancel_error, exc)
        else:
            self.call_from_thread(self._finish_cancel, result)

    def _finish_result(self, result: ExplorerRunResult) -> None:
        self.busy = False
        self._update_status()
        self._set_notice(result.status)
        if result.dataframe is None:
            self.close_results(focus_editor=False)
            return
        self.show_dataframe(result.dataframe)

    def _finish_error(self, exc: Exception) -> None:
        self.busy = False
        self._update_status()
        self.show_error(exc)

    def _finish_cancel(self, result: ExplorerCancelResult) -> None:
        self.cancelling = False
        self._set_notice(result.status)
        self._update_status()
        if self._exit_after_cancel:
            self.exit()

    def _finish_cancel_error(self, exc: Exception) -> None:
        self.cancelling = False
        self._update_status()
        if self._exit_after_cancel:
            self.exit()
        else:
            self.show_error(exc)

    def show_dataframe(self, dataframe: pd.DataFrame) -> None:
        table = self.query_one("#result-table", ResultTable)
        table.clear(columns=True)
        for index, column in enumerate(dataframe.columns):
            table.add_column(str(column), key=f"column-{index}")
        for row in dataframe.itertuples(index=False, name=None):
            table.add_row(*(_format_cell(value) for value in row))
        self.query_one("#result-message", ResultMessage).styles.display = "none"
        table.styles.display = "block"
        self._open_results()

    def show_message(self, message: str) -> None:
        table = self.query_one("#result-table", ResultTable)
        table.styles.display = "none"
        result_message = self.query_one("#result-message", ResultMessage)
        result_message.update(message)
        result_message.styles.display = "block"
        self._open_results()

    def show_error(self, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        self._set_notice("SQL explorer operation failed.")
        self.show_message(message)

    def show_notice(self, message: str) -> None:
        self._set_notice(message)

    def _open_results(self) -> None:
        self.query_one("#result-pane", Vertical).styles.display = "block"
        self.results_open = True

    def close_results(self, *, focus_editor: bool = True) -> None:
        pane = self.query_one("#result-pane", Vertical)
        pane.styles.display = "none"
        self.query_one("#result-table", ResultTable).clear(columns=True)
        self.query_one("#result-message", ResultMessage).update("")
        self.results_open = False
        if focus_editor:
            self.query_one("#query-editor", SqlEditor).focus()

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
        self._handle_command(parts)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if isinstance(event.text_area, SqlEditor):
            event.text_area.refresh_search_matches()

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
            "help": self._command_help,
            "mode": self._command_mode,
            "quit": self._command_exit,
            "run": self._command_run,
            "shortcut": self._command_shortcut,
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

    def _command_mode(self, arguments: list[str]) -> None:
        if not arguments or arguments == ["exploratory"]:
            self._set_notice("Current mode: exploratory.")
            return
        self.show_error(SqlExplorerConfigurationError("Only exploratory mode is available."))

    def _command_help(self, arguments: list[str]) -> None:
        if arguments:
            self.show_error(SqlExplorerConfigurationError("Usage: help"))
            return
        self.show_message(_HELP_TEXT)

    def _command_exit(self, arguments: list[str]) -> None:
        if arguments:
            self.show_error(SqlExplorerConfigurationError("Usage: exit"))
        elif self.busy:
            self._request_cancel(exit_after=True)
        else:
            self.exit()

    def _command_cancel(self, arguments: list[str]) -> None:
        if arguments:
            self.show_error(SqlExplorerConfigurationError("Usage: cancel"))
            return
        self._request_cancel(exit_after=False)

    def _request_cancel(self, *, exit_after: bool) -> None:
        if not self.busy:
            if exit_after:
                self.exit()
            else:
                self._set_notice("No SQL operation is running.")
            return
        if self.cancelling:
            self._exit_after_cancel = self._exit_after_cancel or exit_after
            self._set_notice("Cancellation is already in progress.")
            return
        self.cancelling = True
        self._exit_after_cancel = exit_after
        self._set_notice("Cancelling the active explorer query...")
        self._update_status()
        self._cancel_in_worker()

    def _command_database(self, arguments: list[str]) -> None:
        if len(arguments) != 1:
            self.show_error(SqlExplorerConfigurationError("Usage: db DB_KEY"))
            return
        if self.busy:
            self._set_notice("Wait for the current SQL operation before switching databases.")
            return
        try:
            database = self.session.switch_database(arguments[0])
        except Exception as exc:  # noqa: BLE001 -- validation errors are rendered in the TUI.
            self.show_error(exc)
            return
        self.close_results(focus_editor=False)
        self._update_status()
        self._set_notice(f"Switched to {database.connection_key} ({database.backend}).")

    def _command_shortcut(self, arguments: list[str]) -> None:
        if len(arguments) != 1:
            self.show_error(SqlExplorerConfigurationError("Usage: shortcut KEY|reset"))
            return
        try:
            settings = self.session.set_run_binding(arguments[0])
        except Exception as exc:  # noqa: BLE001 -- settings errors are rendered in the TUI.
            self.show_error(exc)
            return
        self._install_primary_binding(settings.run_binding)
        self._update_status()
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
        self._update_status()
        state = "on" if settings.confirm_mutations else "off"
        self._set_notice(f"Mutation confirmation is {state}.")

    def _command_clear(self, arguments: list[str]) -> None:
        if len(arguments) != 1 or arguments[0].lower() not in {"query", "results", "all"}:
            self.show_error(SqlExplorerConfigurationError("Usage: clear query|results|all"))
            return
        target = arguments[0].lower()
        if target in {"query", "all"}:
            self.query_one("#query-editor", SqlEditor).text = ""
        if target in {"results", "all"}:
            self.close_results()
        self._set_notice(f"Cleared {target}.")

    def _install_primary_binding(self, binding: str) -> None:
        if self._primary_binding and self._primary_binding != "f5":
            self._bindings.keys.pop(self._primary_binding, None)
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
        self._clipboard = value
        with suppress(pyperclip.PyperclipException):
            pyperclip.copy(value)

    def paste_from_explorer_clipboard(self) -> str:
        try:
            value = pyperclip.paste()
        except pyperclip.PyperclipException:
            return self._clipboard
        return value or self._clipboard

    def _update_status(self) -> None:
        confirmation = "on" if self.session.settings.confirm_mutations else "off"
        state = "cancelling" if self.cancelling else "busy" if self.busy else "ready"
        self.query_one("#session-status", Static).update(
            f"db={self.session.database.connection_key} "
            f"backend={self.session.database.backend} mode=exploratory "
            f"run={self.session.settings.run_binding} confirm={confirmation} state={state}"
        )

    def _set_notice(self, message: str | None) -> None:
        self.query_one("#notice", Static).update(message or "")


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
