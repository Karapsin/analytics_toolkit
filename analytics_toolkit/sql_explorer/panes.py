"""Focusable command and result-pane widgets."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from textual.binding import Binding, BindingType
from textual.widgets import Input, OptionList, Static

from .command_completion import command_suggestions
from .inputs import EditableInput

if TYPE_CHECKING:
    from textual import events
    from textual.dom import DOMNode

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
    database_keys: tuple[str, ...] = ()
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "focus_previous_pane", "Previous pane", show=False),
        Binding("down", "focus_next_pane", "Next pane", show=False),
    ]

    @property
    def completion_menu(self) -> OptionList:
        return cast("DOMNode", self.parent).query_one("#command-completion", OptionList)

    def close_completion(self) -> None:
        self.completion_menu.display = False

    def request_completion(self) -> None:
        menu = self.completion_menu
        if menu.display:
            self.accept_completion()
            return
        if self.value.lstrip().lstrip(":").startswith("db "):
            app = cast("SqlExplorerApp", self.app)
            try:
                self.database_keys = app.active_workspace.session.database_keys()
            except Exception as exc:  # noqa: BLE001 -- configuration errors belong in the UI.
                app.show_error(exc)
                return
        self.refresh_completion()
        if menu.option_count == 1:
            self.accept_completion()

    def refresh_completion(self) -> None:
        start, suggestions = command_suggestions(
            self.value, self.cursor_position, self.database_keys
        )
        del start
        menu = self.completion_menu
        menu.clear_options().add_options(suggestions)
        menu.highlighted = 0 if suggestions else None
        menu.display = bool(suggestions)
        menu.styles.height = min(10, len(suggestions) + 2)
        menu.styles.offset = (0, -min(10, len(suggestions) + 2))

    def accept_completion(self) -> None:
        menu = self.completion_menu
        if menu.highlighted is not None:
            start, suggestions = command_suggestions(
                self.value, self.cursor_position, self.database_keys
            )
            if menu.highlighted < len(suggestions):
                suggestion = suggestions[menu.highlighted]
                self.value = self.value[:start] + suggestion + self.value[self.cursor_position :]
                self.cursor_position = start + len(suggestion)
        self.close_completion()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input is self and self.completion_menu.display:
            self.refresh_completion()

    async def _on_key(self, event: events.Key) -> None:
        menu = self.completion_menu
        if menu.display and event.key in {"up", "down", "enter"}:
            event.stop()
            event.prevent_default()
            if event.key == "enter":
                self.accept_completion()
            elif event.key == "up":
                menu.action_cursor_up()
            else:
                menu.action_cursor_down()
            return
        if event.key in {"left", "right", "home", "end"}:
            self.close_completion()
        await super()._on_key(event)

    def on_blur(self) -> None:
        self.close_completion()

    def action_focus_previous_pane(self) -> None:
        cast("SqlExplorerApp", self.app).action_focus_previous_pane()

    def action_focus_next_pane(self) -> None:
        cast("SqlExplorerApp", self.app).action_focus_next_pane()


__all__ = ["CommandInput", "ResultMessage"]
