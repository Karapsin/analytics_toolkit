"""Focusable command and result-pane widgets."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from textual.binding import Binding, BindingType
from textual.widgets import Static

from .inputs import EditableInput

if TYPE_CHECKING:
    from .app import SqlExplorerApp


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


class CommandInput(EditableInput):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "focus_previous_pane", "Previous pane", show=False),
        Binding("down", "focus_next_pane", "Next pane", show=False),
    ]

    def action_focus_previous_pane(self) -> None:
        cast("SqlExplorerApp", self.app).action_focus_previous_pane()

    def action_focus_next_pane(self) -> None:
        cast("SqlExplorerApp", self.app).action_focus_next_pane()


__all__ = ["CommandInput", "ResultMessage"]
