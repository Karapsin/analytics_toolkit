"""Command helpers for the SQL Explorer application."""
# ruff: noqa: SLF001

from __future__ import annotations

from typing import Any, cast

from .editor_actions import positive_count
from .errors import SqlExplorerConfigurationError
from .formatting import format_editor
from .help_text import HELP_TEXT
from .movement import move_command

__all__ = ["HELP_TEXT", "SqlExplorerCursorCommandsMixin"]
_RESULTS_ARGUMENT_COUNT = 2


class SqlExplorerCursorCommandsMixin:
    """The new editor-navigation command surface, kept outside the app shell."""

    def _command_format(self, arguments: list[str]) -> None:
        app = cast("Any", self)
        if arguments:
            app.show_error(SqlExplorerConfigurationError("Usage: format"))
            return
        workspace = app.active_workspace
        try:
            changed = format_editor(workspace.editor, workspace.session.database.backend)
        except ValueError as exc:
            app.show_error(exc, workspace)
            return
        app._set_notice("Formatted SQL." if changed else "SQL unchanged.", workspace)

    def _command_navigation(self, command: str, arguments: list[str]) -> None:

        app = cast("Any", self)
        try:
            move_command(app.active_workspace.editor, command, arguments)
        except ValueError as exc:
            app.show_error(SqlExplorerConfigurationError(str(exc)))
            return
        app._update_editor_status()

    def _command_move(self, arguments: list[str]) -> None:
        self._command_navigation("mv", arguments)

    def _command_move_select(self, arguments: list[str]) -> None:
        self._command_navigation("mvs", arguments)

    def _command_delete(self, arguments: list[str]) -> None:
        app = cast("Any", self)
        if arguments:
            app.show_error(SqlExplorerConfigurationError("Usage: del"))
            return
        app.active_workspace.editor.action_delete_right()

    def _command_copy(self, arguments: list[str]) -> None:
        app = cast("Any", self)
        if arguments:
            app.show_error(SqlExplorerConfigurationError("Usage: cp"))
            return
        editor = app.active_workspace.editor
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
        editor = app.active_workspace.editor
        if editor.paste_clipboard(app.paste_from_explorer_clipboard()):
            app._set_notice(f"Pasted at {editor.cursor_count} cursor(s).")
        else:
            app._set_notice("Clipboard is empty.")

    def action_toggle_keyboard(self) -> None:
        self._command_keyboard(["toggle"])

    def _command_keyboard(self, arguments: list[str]) -> None:
        app = cast("Any", self)
        if arguments not in ([], ["on"], ["off"], ["toggle"]):
            app.show_error(SqlExplorerConfigurationError("Usage: keyboard [on|off|toggle]"))
            return
        if arguments:
            app.keyboard_mode = (
                not app.keyboard_mode if arguments == ["toggle"] else arguments == ["on"]
            )
            if app.keyboard_mode:
                app.capture_mouse(None)
            for workspace in app._workspaces.values():
                app._update_editor_status(workspace)
        app._set_notice(f"Keyboard mode: {'on' if app.keyboard_mode else 'off'}.")

    def _command_results(self, arguments: list[str]) -> None:

        app = cast("Any", self)

        workspace = cast("Any", self).active_workspace
        if arguments == ["switch"]:
            workspace.results_orientation = (
                "vertical" if workspace.results_orientation == "horizontal" else "horizontal"
            )
        elif len(arguments) == _RESULTS_ARGUMENT_COUNT and arguments[0] in {"expand", "shrink"}:
            try:
                count = positive_count(arguments[1])
            except ValueError as exc:
                app.show_error(SqlExplorerConfigurationError(str(exc)))
                return
            pane = workspace.query_one(".result-pane")
            size = (
                pane.region.width
                if workspace.results_orientation == "vertical"
                else pane.region.height
            )
            base = (
                size
                if workspace.results_open
                else workspace.result_sizes[workspace.results_orientation]
            )
            if base is None:
                split = workspace.query_one(".results-split")
                base = (
                    (
                        split.size.width
                        if workspace.results_orientation == "vertical"
                        else split.size.height
                    )
                    - 1
                ) // 2
            workspace.result_sizes[workspace.results_orientation] = base + count * (
                1 if arguments[0] == "expand" else -1
            )
        else:
            app.show_error(
                SqlExplorerConfigurationError(
                    "Usage: results switch | results expand N | results shrink N"
                )
            )
            return
        workspace.apply_results_layout()
        app._set_notice(f"Results layout: {workspace.results_orientation}.")
