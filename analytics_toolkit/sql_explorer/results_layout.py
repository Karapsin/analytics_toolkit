"""Keyboard and mouse control of the editor/results split."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from textual.containers import Vertical
from textual.widgets import Static

if TYPE_CHECKING:
    from textual import events


class ResultsSeparator(Static):
    _drag_origin: int | None = None
    _initial_size = 0

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if cast("Any", self.app).keyboard_mode or event.button != 1:
            return
        from .workspace import workspace_for  # noqa: PLC0415 -- workspace/widget cycle.

        workspace = workspace_for(self)
        vertical = workspace.results_orientation == "vertical"
        self._drag_origin = event.screen_x if vertical else event.screen_y
        pane = workspace.query_one(".result-pane")
        self._initial_size = pane.region.width if vertical else pane.region.height
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag_origin is None or cast("Any", self.app).keyboard_mode:
            return
        from .workspace import workspace_for  # noqa: PLC0415 -- workspace/widget cycle.

        workspace = workspace_for(self)
        position = event.screen_x if workspace.results_orientation == "vertical" else event.screen_y
        workspace.result_sizes[workspace.results_orientation] = (
            self._initial_size + self._drag_origin - position
        )
        workspace.apply_results_layout()
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self._drag_origin = None
        self.release_mouse()
        event.stop()


class ResultsSplit(Vertical):
    def on_resize(self, _event: events.Resize) -> None:
        from .workspace import workspace_for  # noqa: PLC0415 -- workspace/widget cycle.

        workspace_for(self).apply_results_layout()
