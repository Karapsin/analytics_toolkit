"""Command helpers for the SQL Explorer application."""
# ruff: noqa: SLF001

from __future__ import annotations

from typing import Any, cast

from .editor import SqlEditor
from .errors import SqlExplorerConfigurationError

HELP_TEXT = """Commands
  run                         execute the editor
  open                        open remote-host SQL file navigation
  save                        save the opened SQL file
  cancel                      cancel the active explorer query
  mode [exploratory|navigation]
                              show or enter a mode
  mv LINE_NUMBER              move to the one-based line start
  mvs LINE_NUMBER             select to the one-based line start
  cp                          copy selections or the editor buffer
  pst                         paste at every editor cursor
  db DB_KEY                   switch the configured connection
  shortcut KEY|reset          change the primary run shortcut
  confirm on|off|toggle       control mutation confirmation
  clear query|results|all     clear workspace content
  help                        show this help
  exit | quit                 close the explorer

Keys
  Ctrl+O                      open remote-host SQL file navigation
  Ctrl+S                      save the opened SQL file
  Ctrl+N                      name and create a blank SQL file
  Cmd+O                       optional terminal-forwarded open shortcut
  Alt+Tab                     optionally cycle panes
  Alt+Shift+Tab               cycle panes in reverse
  Up / Down                   cross pane boundaries or navigate Find/Replace
  Left / Right                choose a visible confirmation action
  Shift+Up / Shift+Down       add or remove editor cursors
  Ctrl+Enter                  default run shortcut
  Fn+Enter                    run when reported as keypad Enter
  Cmd+Enter                   run when forwarded by a macOS terminal
  F5                          permanent run fallback
  Ctrl+F                      find and replace in the editor
  Delete                      close a focused result/error pane
  Escape                      close overlays, collapse cursors, or move panes
  Interrupt                   request cancellation of the active query
  Ctrl+C                      copy editor or result selection
  Tab                         complete SQL or indent when unavailable
"""


class SqlExplorerCursorCommandsMixin:
    """The new editor-navigation command surface, kept outside the app shell."""

    def _command_line_number(self, command: str, arguments: list[str]) -> int | None:
        app = cast("Any", self)
        if len(arguments) != 1:
            app.show_error(SqlExplorerConfigurationError(f"Usage: {command} LINE_NUMBER"))
            return None
        try:
            line_number = int(arguments[0])
        except ValueError:
            app.show_error(SqlExplorerConfigurationError("Line number must be a positive integer."))
            return None
        editor = app.query_one(SqlEditor)
        if line_number < 1 or line_number > editor.document.line_count:
            app.show_error(
                SqlExplorerConfigurationError(
                    f"Line number must be between 1 and {editor.document.line_count}."
                )
            )
            return None
        return line_number

    def _command_move(self, arguments: list[str]) -> None:
        app = cast("Any", self)
        if (line_number := app._command_line_number("mv", arguments)) is not None:
            app.query_one(SqlEditor).move_to_line_start(line_number)
            app._set_notice(f"Moved to line {line_number}.")

    def _command_move_select(self, arguments: list[str]) -> None:
        app = cast("Any", self)
        if (line_number := app._command_line_number("mvs", arguments)) is not None:
            app.query_one(SqlEditor).select_to_line_start(line_number)
            app._set_notice(f"Selected to line {line_number}.")

    def _command_copy(self, arguments: list[str]) -> None:
        app = cast("Any", self)
        if arguments:
            app.show_error(SqlExplorerConfigurationError("Usage: cp"))
            return
        editor = app.query_one(SqlEditor)
        app.copy_to_explorer_clipboard(editor.command_copy_text())
        app._set_notice(
            "Copied editor selection."
            if any(not item.is_empty for item in editor.cursor_selections)
            else "Copied editor."
        )

    def _command_paste(self, arguments: list[str]) -> None:
        app = cast("Any", self)
        if arguments:
            app.show_error(SqlExplorerConfigurationError("Usage: pst"))
            return
        editor = app.query_one(SqlEditor)
        if editor.paste_clipboard(app.paste_from_explorer_clipboard()):
            app._set_notice(f"Pasted at {editor.cursor_count} cursor(s).")
        else:
            app._set_notice("Clipboard is empty.")
