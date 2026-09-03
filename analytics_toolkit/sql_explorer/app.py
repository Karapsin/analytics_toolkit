from __future__ import annotations

import shlex
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pyperclip
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Button, Input, OptionList, Static, TextArea

from .clipboard import TerminalClipboard
from .completion import (
    MIN_TABLE_PREFIX_LENGTH,
    CompletionContext,
    CompletionCoordinator,
    CompletionResult,
    filter_suggestions,
    keyword_suggestions,
    parse_completion_context,
)
from .errors import SqlExplorerConfigurationError
from .filetree import read_sql_file
from .picker import DatabasePickerApp
from .runtime import format_duration
from .styles import APP_CSS
from .widgets import (
    CommandInput,
    CompletionMenu,
    ConfirmMutationScreen,
    DiscardChangesScreen,
    FileNavigationScreen,
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
  open                        open remote-host SQL file navigation
  cancel                      cancel the active explorer query
  mode [exploratory|navigation]
                              show or enter a mode
  db DB_KEY                   switch the configured connection
  shortcut KEY|reset          change the primary run shortcut
  confirm on|off|toggle       control mutation confirmation
  clear query|results|all     clear workspace content
  help                        show this help
  exit | quit                 close the explorer

Keys
  Ctrl+O                      open remote-host SQL file navigation
  Cmd+O                       optional terminal-forwarded open shortcut
  Alt+Tab                     optionally cycle panes
  Alt+Shift+Tab               cycle panes in reverse
  Up / Down                   cross pane boundaries
  Ctrl+Enter                  default run shortcut
  Fn+Enter                    run when reported as keypad Enter
  Cmd+Enter                   run when forwarded by a macOS terminal
  F5                          permanent run fallback
  Ctrl+F                      find and replace in the editor
  Delete                      close a focused result/error pane
  Escape                      close overlays or move forward through panes
  Interrupt                   request cancellation of the active query
  Ctrl+C                      copy editor or result selection
  Tab                         complete SQL or indent when unavailable

Notes
  Selected SQL runs instead of the complete buffer.
  Navigation reads the remote host's current directory and never writes files.
  OSC 52 copy targets the SSH client's clipboard when the terminal permits it.
"""

_SLOW_QUERY_SECONDS = 300


def _remove_dynamic_binding(bindings: Any, key: str) -> None:
    storage = getattr(bindings, "keys", None)
    if storage is None:
        storage = bindings.key_to_bindings
    storage.pop(key, None)


class SqlExplorerApp(App[None]):
    TITLE = "analytics-toolkit SQL explorer"
    CSS = APP_CSS
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f5", "run_query", "Run", priority=True),
        Binding("ctrl+o", "open_navigation", "Open SQL file", priority=True),
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
        Binding("escape", "escape", "Next pane", show=False),
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
        self._terminal_clipboard = TerminalClipboard()
        self._completion: CompletionCoordinator | None = None
        self._completion_context: CompletionContext | None = None
        self._current_file: Path | None = None
        self._saved_text = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="workspace"):
            with Vertical(id="query-pane"):
                yield SqlEditor(
                    id="query-editor",
                    show_line_numbers=True,
                    soft_wrap=False,
                )
                yield FindReplaceBar(id="find-replace-bar")
                yield CompletionMenu(id="completion-menu", wrap=False)
            with Vertical(id="result-pane"):
                yield ResultTable(id="result-table", cursor_type="cell", zebra_stripes=True)
                yield ResultMessage("", id="result-message", markup=False)
        with Vertical(id="command-panel"):
            yield Static("", id="session-status", markup=False)
            yield Static("", id="notice", markup=False)
            yield Button("Interrupt", id="interrupt", disabled=True)
            yield CommandInput(placeholder=": command", id="command-input")

    def on_mount(self) -> None:
        self.query_one("#result-message", ResultMessage).styles.display = "none"
        self.query_one(CompletionMenu).styles.display = "none"
        self._install_primary_binding(self.session.settings.run_binding)
        self._start_completion_coordinator()
        self._update_status()
        if self.session.settings_warning:
            self._set_notice(self.session.settings_warning)
        self.query_one("#query-editor", SqlEditor).focus()
        self.set_interval(1.0, self._update_status)

    def on_unmount(self) -> None:
        if self._completion is not None:
            self._completion.stop()

    def action_run_query(self) -> None:
        if self.busy:
            self._set_notice("A SQL operation is already running.")
            return
        editor = self.query_one("#query-editor", SqlEditor)
        sql_text = editor.selected_text if not editor.selection.is_empty else editor.text
        try:
            plan = self.session.plan(sql_text)
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

    def action_escape(self) -> None:
        # Modal screens own Escape; the app-level priority binding must not
        # dismiss or navigate behind them.
        if len(self.screen_stack) > 1:
            return
        bar = self.query_one(FindReplaceBar)
        if bar.styles.display != "none":
            bar.action_close()
            return
        menu = self.query_one(CompletionMenu)
        if menu.is_open:
            menu.action_close()
            return
        self.action_focus_next_pane()

    def action_plain_tab(self) -> None:
        menu = self.query_one(CompletionMenu)
        if menu.is_open:
            self._accept_completion()
            return
        focused = self.focused
        if isinstance(focused, SqlEditor) and not self._request_completion():
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
        elif isinstance(focused, ResultTable):
            value = focused.copy_text()
            if value:
                self.copy_to_explorer_clipboard(value)
                self._set_notice("Copied selection.")

    def action_open_find(self) -> None:
        self.query_one(FindReplaceBar).open()

    def action_open_navigation(self) -> None:
        if len(self.screen_stack) > 1:
            return
        self.push_screen(FileNavigationScreen(Path.cwd()), self._navigation_closed)

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
        self.cancelling = False
        self._update_status()
        self._set_notice(result.status)
        if self._exit_after_cancel:
            self.exit()
            return
        if result.dataframe is None:
            self.close_results(focus_editor=False)
            return
        self.show_dataframe(result.dataframe)

    def _finish_error(self, exc: Exception) -> None:
        self.busy = False
        self.cancelling = False
        self._update_status()
        if self._exit_after_cancel:
            self.exit()
            return
        self.show_error(exc)

    def _finish_cancel(self, result: ExplorerCancelResult) -> None:
        self._set_notice(result.status)
        self._update_status()

    def _finish_cancel_error(self, exc: Exception) -> None:
        self.cancelling = False
        self._exit_after_cancel = False
        self._update_status()
        self.show_error(exc)

    def show_dataframe(self, dataframe: pd.DataFrame) -> None:
        table = self.query_one("#result-table", ResultTable)
        table.clear_rectangular_selection()
        table.clear(columns=True)
        for index, column in enumerate(dataframe.columns):
            table.add_column(Text(str(column)), key=f"column-{index}")
        for row_number, row in enumerate(dataframe.itertuples(index=False, name=None), 1):
            table.add_row(
                *(Text(_format_cell(value)) for value in row),
                label=str(row_number),
            )
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
        table = self.query_one("#result-table", ResultTable)
        table.clear_rectangular_selection()
        table.clear(columns=True)
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
            if len(self.screen_stack) > 1:
                return
            self._refresh_open_completion()
            self._update_status()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if isinstance(event.option_list, CompletionMenu):
            event.stop()
            self._accept_completion()

    def load_sql_file(self, path: str | Path) -> None:
        resolved = Path(path).resolve()
        editor = self.query_one("#query-editor", SqlEditor)
        if editor.text != self._saved_text:
            self.push_screen(
                DiscardChangesScreen(resolved),
                lambda discard: self._load_sql_file_now(resolved) if discard else None,
            )
            return
        self._load_sql_file_now(resolved)

    def _load_sql_file_now(self, path: Path) -> None:
        try:
            text = read_sql_file(path)
        except (OSError, UnicodeError) as exc:
            self.show_message(f"{type(exc).__name__}: {exc}")
            return
        editor = self.query_one("#query-editor", SqlEditor)
        editor.text = text
        editor.cursor_location = (0, 0)
        self._current_file = path
        self._saved_text = text
        self._set_notice(f"Loaded {path}")
        self._update_status()
        editor.focus()

    def _navigation_closed(self, path: Path | None) -> None:
        if path is not None:
            self.load_sql_file(path)

    def _start_completion_coordinator(self) -> None:
        if self._completion is not None:
            self._completion.stop()
        database = self.session.database
        self._completion = CompletionCoordinator(
            database.connection_key,
            database.backend,
        )
        self._completion.start_bootstrap(on_error=self._metadata_error_from_thread)

    def _completion_at_cursor(self) -> CompletionContext:
        editor = self.query_one(SqlEditor)
        row, column = editor.cursor_location
        lines = editor.text.splitlines(keepends=True)
        offset = sum(len(line) for line in lines[:row]) + column
        coordinator = self._completion
        catalogs = coordinator.known_catalogs() if coordinator is not None else None
        return parse_completion_context(
            editor.text,
            offset,
            backend=self.session.database.backend,
            connection_key=self.session.database.connection_key,
            trino_catalogs=catalogs,
        )

    def _request_completion(self) -> bool:
        editor = self.query_one(SqlEditor)
        if not editor.selection.is_empty:
            return False
        context = self._completion_at_cursor()
        if context.request.kind == "keyword":
            suggestions = keyword_suggestions(context.request.prefix)
            handled = bool(context.request.prefix and suggestions)
            if handled:
                self._open_completion(context, suggestions)
            return handled

        coordinator = self._completion
        if coordinator is None:
            return False
        self._completion_context = context
        opened = self._open_namespace_completion(context)
        cached = coordinator.cached(context.request)
        if cached is not None:
            if cached:
                self._open_completion(context, cached)
            return True
        if len(context.request.prefix) < MIN_TABLE_PREFIX_LENGTH:
            if not opened:
                self._set_notice(f"Type at least {MIN_TABLE_PREFIX_LENGTH} table-name characters.")
            return opened
        coordinator.enqueue(
            replace(
                context.request,
                prefix=context.request.prefix[:MIN_TABLE_PREFIX_LENGTH],
            ),
            on_success=self._completion_from_thread,
            on_error=self._metadata_error_from_thread,
        )
        self._set_notice("Loading matching table names...")
        return True

    def _open_namespace_completion(self, context: CompletionContext) -> bool:
        coordinator = self._completion
        if coordinator is None:
            return False
        request = context.request
        values: tuple[str, ...] | None
        if request.backend == "trino" and request.catalog is None:
            values = coordinator.known_catalogs()
        elif request.backend == "trino" and request.schema is None:
            values = coordinator.cached_schemas(request.catalog)
        elif request.schema is None:
            values = coordinator.cached_schemas(None)
            if values is None:
                coordinator.enqueue_schemas(
                    on_success=self._namespace_from_thread,
                    on_error=self._metadata_error_from_thread,
                )
        else:
            values = None
        suggestions = filter_suggestions(values or (), request.prefix)
        if suggestions:
            self._open_completion(context, suggestions)
            return True
        return values is None

    def _completion_from_thread(self, result: CompletionResult) -> None:
        with suppress(RuntimeError):
            self.call_from_thread(self._receive_completion, result)

    def _namespace_from_thread(self, result: CompletionResult) -> None:
        with suppress(RuntimeError):
            self.call_from_thread(self._receive_namespace, result)

    def _metadata_error_from_thread(
        self,
        _result: CompletionResult,
        exc: Exception,
    ) -> None:
        with suppress(RuntimeError):
            self.call_from_thread(
                self._set_notice,
                f"Metadata completion unavailable: {type(exc).__name__}: {exc}",
            )

    def _receive_completion(self, result: CompletionResult) -> None:
        context = self._completion_at_cursor()
        if context.request.scope != result.request.scope:
            return
        coordinator = self._completion
        suggestions = coordinator.cached(context.request) if coordinator else None
        if suggestions:
            self._open_completion(context, suggestions)
        else:
            self.query_one(CompletionMenu).action_close()
            self._set_notice("No matching table names found.")

    def _receive_namespace(self, _result: CompletionResult) -> None:
        context = self._completion_at_cursor()
        if context.request.kind == "table":
            self._open_namespace_completion(context)

    def _open_completion(
        self,
        context: CompletionContext,
        suggestions: tuple[str, ...],
    ) -> None:
        self._completion_context = context
        menu = self.query_one(CompletionMenu)
        editor = self.query_one(SqlEditor)
        cursor_x, cursor_y = editor.cursor_render_offset
        menu.styles.offset = (cursor_x + editor.gutter_width + 1, cursor_y + 1)
        menu.open(suggestions)

    def _accept_completion(self) -> None:
        menu = self.query_one(CompletionMenu)
        suggestion = menu.selected_suggestion()
        if suggestion is None:
            menu.action_close()
            return
        context = self._completion_at_cursor()
        editor = self.query_one(SqlEditor)
        start = self._offset_to_location(editor.text, context.replacement_start)
        end = self._offset_to_location(editor.text, context.replacement_end)
        result = editor.replace(suggestion, start, end, maintain_selection_offset=False)
        editor.cursor_location = result.end_location
        menu.action_close()

    def _refresh_open_completion(self) -> None:
        menu = self.query_one(CompletionMenu)
        if not menu.is_open:
            return
        previous = self._completion_context
        context = self._completion_at_cursor()
        if previous is None or previous.request.scope != context.request.scope:
            menu.action_close()
            return
        suggestions: tuple[str, ...] | None
        if context.request.kind == "keyword":
            suggestions = keyword_suggestions(context.request.prefix)
        else:
            coordinator = self._completion
            suggestions = coordinator.cached(context.request) if coordinator else None
            if suggestions is None:
                return
        if suggestions:
            self._open_completion(context, suggestions)
        else:
            menu.action_close()

    @staticmethod
    def _offset_to_location(text: str, offset: int) -> tuple[int, int]:
        before = text[:offset]
        row = before.count("\n")
        column = len(before.rsplit("\n", 1)[-1])
        return row, column

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
            "open": self._command_open,
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "interrupt":
            self._request_cancel(exit_after=False)

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
        self._start_completion_coordinator()
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

    def _update_status(self) -> None:
        confirmation = "on" if self.session.settings.confirm_mutations else "off"
        state = "cancelling" if self.cancelling else "busy" if self.busy else "ready"
        status = (
            f"db={self.session.database.connection_key} "
            f"backend={self.session.database.backend} mode=exploratory "
        )
        editor = self.query_one("#query-editor", SqlEditor)
        if self._current_file is not None:
            dirty = "*" if editor.text != self._saved_text else ""
            status += f"file={self._current_file}{dirty} "
        elif editor.text:
            status += "file=<unsaved>* "
        query = getattr(self.session, "active_query", None) or getattr(
            self.session,
            "last_query",
            None,
        )
        if query is not None:
            duration = format_duration(query.elapsed_seconds)
            status += f"query={query.label} route=sql.{query.route.value} elapsed={duration}"
            suffix = (
                f" run={self.session.settings.run_binding} confirm={confirmation} state={state}"
            )
            if query.elapsed_seconds >= _SLOW_QUERY_SECONDS:
                warning = Text(" consider optimizing your query or sit tight", style="bold red")
                status_text = Text(status + suffix)
                status_text.append(warning)
                self.query_one("#session-status", Static).update(status_text)
            else:
                self.query_one("#session-status", Static).update(status + suffix)
        else:
            suffix = f"run={self.session.settings.run_binding} confirm={confirmation} state={state}"
            self.query_one("#session-status", Static).update(status + suffix)
        interrupt = self.query_one("#interrupt", Button)
        interrupt.disabled = not self.busy or self.cancelling

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
