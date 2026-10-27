"""Open the preferences form using the active tab's layout."""
# ruff: noqa: SLF001 -- application mixin shares the app's internal state.

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

from analytics_toolkit.general.connections import (
    get_connections_path_override,
    get_last_connections_path,
)

from .errors import SqlExplorerConfigurationError
from .preferences import _size, apply_preferences
from .settings_screen import SettingsScreen


class SqlExplorerSettingsCommandsMixin:
    def _command_settings(self, arguments: list[str]) -> None:
        app = cast("Any", self)
        if arguments:
            app.show_error(SqlExplorerConfigurationError("Usage: settings"))
            return
        if len(app.screen_stack) > 1:
            return
        workspace = app.active_workspace
        settings = replace(
            workspace.session.settings,
            keyboard_mode=app.keyboard_mode,
            results_orientation=workspace.results_orientation,
            horizontal_size=_size(workspace.result_sizes["horizontal"]),
            vertical_size=_size(workspace.result_sizes["vertical"]),
        )

        def closed(result: str | None) -> None:
            workspace.command_input.focus()
            if result is not None:
                app._set_notice("Settings saved.", workspace)
            if result == "connections":
                app._command_connections([])

        app.push_screen(
            SettingsScreen(
                settings,
                lambda updated: apply_preferences(app, workspace, updated),
                get_connections_path_override() or get_last_connections_path(),
            ),
            closed,
        )
