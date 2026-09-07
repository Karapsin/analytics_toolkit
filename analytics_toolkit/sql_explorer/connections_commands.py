"""Switch the entire Explorer only after its previous SQL work has stopped."""
# ruff: noqa: SLF001 -- application mixin shares the app's internal state.

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.general.connections import (
    get_connections_path_override,
    get_last_connections_path,
)

from .connections import ConnectionsRestart
from .connections_picker import ConnectionsPickerScreen
from .errors import SqlExplorerConfigurationError

if TYPE_CHECKING:
    from pathlib import Path

_SHUTDOWN_TIMEOUT_SECONDS = 30


class SqlExplorerConnectionsCommandsMixin:
    def _command_connections(self, arguments: list[str]) -> None:
        app = cast("Any", self)
        if arguments:
            app.show_error(SqlExplorerConfigurationError("Usage: connections"))
            return
        if app._exit_requested or len(app.screen_stack) > 1:
            return
        current = get_connections_path_override() or get_last_connections_path()

        def selected(path: Path | None) -> None:
            if path is not None and path != current:
                app._request_exit(restart=ConnectionsRestart(path))

        app.push_screen(ConnectionsPickerScreen(current), selected)

    def _finish_connections_shutdown(self) -> None:
        app = cast("Any", self)
        if app._connections_stop_started is None:
            app._connections_stop_started = monotonic()
            app._completion_pool.stop()
            app._connections_stop_timer = app.set_interval(0.1, app._finish_exit_if_ready)
        workers_finished = all(worker.is_finished for worker in app.workers)
        if app._completion_pool.is_stopped and workers_finished:
            app._connections_stop_timer.stop()
            app.exit(app._connections_restart)
        elif monotonic() - app._connections_stop_started >= _SHUTDOWN_TIMEOUT_SECONDS:
            app._connections_stop_timer.stop()
            app._connections_stop_started = None
            app._connections_restart = None
            app._exit_requested = False
            for workspace in app._workspaces.values():
                app._start_completion_coordinator(workspace)
            app.show_error(
                SqlExplorerConfigurationError(
                    "Metadata is still stopping; the .connections file has not changed. "
                    "Try connections again after it finishes."
                )
            )
        else:
            app._set_notice("Waiting for metadata workers before switching .connections…")
