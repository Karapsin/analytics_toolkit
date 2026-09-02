from __future__ import annotations

from typing import ClassVar, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class DatabasePickerApp(App[Optional[str]]):
    TITLE = "analytics-toolkit SQL explorer"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q", "cancel", "Cancel", show=False),
    ]

    CSS = """
    Screen {
        align: center middle;
    }
    #database-picker {
        width: 70%;
        max-width: 80;
        height: 70%;
        max-height: 30;
        border: round $accent;
    }
    #database-picker-title {
        height: 3;
        padding: 1 2;
    }
    #database-options {
        height: 1fr;
    }
    #database-picker-help {
        height: 2;
        padding: 0 2;
    }
    """

    def __init__(self, choices: tuple[tuple[str, str], ...]) -> None:
        super().__init__()
        self.choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(id="database-picker"):
            yield Static("Select a .connections database key", id="database-picker-title")
            yield OptionList(
                *(Option(f"{key} ({backend})", id=key) for key, backend in self.choices),
                id="database-options",
            )
            yield Static("Enter: select · Esc/Q: exit", id="database-picker-help")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id is not None:
            self.exit(event.option_id)

    def action_cancel(self) -> None:
        self.exit(None)


__all__ = ["DatabasePickerApp"]
