"""Focusable command and result-pane widgets."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.widgets import Input, OptionList, RichLog

from .command_completion import command_suggestions
from .editor_actions import completion_text
from .inputs import EditableInput

if TYPE_CHECKING:
    from textual import events
    from textual.dom import DOMNode

    from .app import SqlExplorerApp


class ResultMessage(RichLog):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "scroll_up", "Scroll up", show=False),
        Binding("down", "scroll_down", "Scroll down", show=False),
        Binding("delete", "close_results", "Close results", show=False),
    ]

    def __init__(self, content: str = "", **kwargs: Any) -> None:
        super().__init__(wrap=True, min_width=1, auto_scroll=False, **kwargs)
        self._message = Text(content)

    def update(self, content: str | Text) -> None:
        self._message = content if isinstance(content, Text) else Text(content)
        self.call_after_refresh(self._write_content)

    def _write_content(self) -> None:
        self.clear()
        if self._message:
            self.write(self._message)

    def on_resize(self) -> None:
        # Textual dispatches base-class resize handlers independently; their
        # signatures differ between supported releases. Write after that dispatch.
        self.call_after_refresh(self._write_content)

    def render(self) -> Text:
        return self._message

    def action_focus_previous_pane(self) -> None:
        cast("SqlExplorerApp", self.app).action_focus_previous_pane()

    def action_focus_next_pane(self) -> None:
        cast("SqlExplorerApp", self.app).action_focus_next_pane()

    def action_close_results(self) -> None:
        cast("SqlExplorerApp", self.app).close_results()


class CommandInput(EditableInput):
    database_keys: tuple[str, ...] = ()
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "history_previous", "Previous command", show=False),
        Binding("down", "history_next", "Next command", show=False),
    ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._commands: list[str] = []
        self._history_index = 0
        self._draft = ("", 0)

    def remember_command(self, value: str) -> None:
        if value.strip():
            self._commands.append(value)
        self._history_index = len(self._commands)
        self._draft = ("", 0)

    def action_history_previous(self) -> None:
        if self._history_index == len(self._commands):
            self._draft = (self.value, self.cursor_position)
        if self._history_index > 0:
            self._history_index -= 1
            self.value = self._commands[self._history_index]
            self.cursor_position = len(self.value)

    def action_history_next(self) -> None:
        if self._history_index < len(self._commands):
            self._history_index += 1
            if self._history_index == len(self._commands):
                self.value, self.cursor_position = self._draft
            else:
                self.value = self._commands[self._history_index]
                self.cursor_position = len(self.value)

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
                suggestion = completion_text(
                    suggestions[menu.highlighted], self.value[self.cursor_position :]
                )
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
