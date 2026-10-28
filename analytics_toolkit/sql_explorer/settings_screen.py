"""Edit Explorer preferences as a draft, publishing them only after a successful save."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, ClassVar, Optional

from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, OptionList, Select, Static

from .create_table_screen import BooleanCheckbox
from .errors import SqlExplorerConfigurationError
from .inputs import EditableInput
from .settings import ExplorerSettings, normalize_run_binding

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from textual.app import ComposeResult


def _result_size(value: str, label: str) -> int | None:
    if not value.strip():
        return None
    try:
        size = int(value)
        if size >= 0:
            return size
    except ValueError:
        pass
    message = f"{label} must be a nonnegative whole number, or blank for automatic sizing."
    raise ValueError(message)


class SettingsScreen(ModalScreen[Optional[str]]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("up", "move_field(-1)", "Previous field", priority=True),
        Binding("down", "move_field(1)", "Next field", priority=True),
    ]
    DEFAULT_CSS = """
    SettingsScreen { align: center middle; background: $background 75%; }
    #settings-dialog {
        width: 88; max-width: 95%; height: 41; max-height: 95%;
        border: solid $accent; background: $panel; padding: 1 2;
    }
    #settings-title { height: 1; text-style: bold; color: $accent; }
    #settings-fields {
        height: 1fr; scrollbar-size-vertical: 1;
        scrollbar-background: $panel; scrollbar-color: $accent-darken-1;
        scrollbar-corner-color: $panel;
    }
    #settings-fields Static { height: auto; }
    .settings-section { margin-top: 1; color: $accent; text-style: bold; }
    .settings-row { height: 3; }
    .settings-row > Static { width: 28; padding-top: 1; }
    .settings-row Input, .settings-row Select { width: 1fr; }
    #settings-fields Checkbox { height: 1; padding: 0; border: none; }
    .settings-hint { color: $text-muted; }
    #settings-notice { height: auto; color: $error; }
    #settings-actions { height: 3; margin-top: 1; align-horizontal: right; }
    #settings-actions Button { min-width: 10; margin-left: 1; }
    #settings-connections { margin-top: 1; }
    """

    def __init__(
        self,
        settings: ExplorerSettings,
        save: Callable[[ExplorerSettings], None],
        connections_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.initial_settings = settings
        self.save_preferences = save
        self.connections_path = connections_path

    def compose(self) -> ComposeResult:
        settings = self.initial_settings
        with Vertical(id="settings-dialog"):
            yield Static("Settings", id="settings-title")
            with VerticalScroll(id="settings-fields"):
                yield Static("Execution", classes="settings-section")
                with Horizontal(classes="settings-row"):
                    yield Static("Run shortcut")
                    yield EditableInput(settings.run_binding, id="settings-run-binding")
                yield Static(
                    "F5 always runs SQL. Reserved editing keys cannot be used.",
                    classes="settings-hint",
                )
                yield BooleanCheckbox(
                    "Confirm SQL changes before running",
                    settings.confirm_mutations,
                    id="settings-confirm",
                )
                yield Static("Editor", classes="settings-section")
                yield BooleanCheckbox(
                    "Keyboard mode (disable editor mouse actions)",
                    settings.keyboard_mode,
                    id="settings-keyboard",
                )
                yield Static("Results layout", classes="settings-section")
                yield Static(
                    "Applies to this tab and becomes the default for new tabs.",
                    classes="settings-hint",
                )
                with Horizontal(classes="settings-row"):
                    yield Static("Position")
                    yield Select(
                        (("Below editor", "horizontal"), ("Right of editor", "vertical")),
                        value=settings.results_orientation,
                        allow_blank=False,
                        id="settings-orientation",
                    )
                with Horizontal(classes="settings-row"):
                    yield Static("Height below (rows)")
                    yield EditableInput(
                        "" if settings.horizontal_size is None else str(settings.horizontal_size),
                        placeholder="Automatic",
                        id="settings-horizontal-size",
                    )
                with Horizontal(classes="settings-row"):
                    yield Static("Width on right (columns)")
                    yield EditableInput(
                        "" if settings.vertical_size is None else str(settings.vertical_size),
                        placeholder="Automatic",
                        id="settings-vertical-size",
                    )
                yield Static(
                    "Leave sizes blank for automatic sizing. Sizes fit the terminal.",
                    classes="settings-hint",
                )
                yield Static("Connections file", classes="settings-section")
                yield Static(
                    str(self.connections_path) if self.connections_path else "No file selected",
                    id="settings-connections-path",
                    markup=False,
                )
                yield Button("Save & change connections", id="settings-connections")
            yield Static("", id="settings-notice", markup=False)
            with Horizontal(id="settings-actions"):
                yield Button("Reset defaults", id="settings-reset")
                yield Button("Cancel", id="settings-cancel")
                yield Button("Save", id="settings-save")

    def on_mount(self) -> None:
        self.query_one("#settings-run-binding", Input).focus()

    def action_next_field(self) -> None:
        self.focus_next()

    def action_previous_field(self) -> None:
        self.focus_previous()

    def action_move_field(self, direction: int) -> None:
        focused = self.app.focused
        if isinstance(focused, OptionList):
            if direction < 0:
                focused.action_cursor_up()
            else:
                focused.action_cursor_down()
        elif direction < 0:
            self.focus_previous()
        else:
            self.focus_next()

    def action_cancel(self) -> None:
        orientation = self.query_one("#settings-orientation", Select)
        if orientation.expanded:
            orientation.expanded = False
            orientation.focus()
            return
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "settings-cancel":
            self.action_cancel()
        elif event.button.id == "settings-reset":
            self._reset_defaults()
        elif event.button.id in {"settings-save", "settings-connections"}:
            self._save(change_connections=event.button.id == "settings-connections")

    def _reset_defaults(self) -> None:
        defaults = ExplorerSettings()
        self.query_one("#settings-run-binding", Input).value = defaults.run_binding
        self.query_one("#settings-confirm", Checkbox).value = defaults.confirm_mutations
        self.query_one("#settings-keyboard", Checkbox).value = defaults.keyboard_mode
        self.query_one("#settings-orientation", Select).value = defaults.results_orientation
        self.query_one("#settings-horizontal-size", Input).value = ""
        self.query_one("#settings-vertical-size", Input).value = ""
        self.query_one("#settings-notice", Static).update(
            "Defaults restored in this form. Save to apply."
        )

    def _save(self, *, change_connections: bool = False) -> None:
        try:
            settings = replace(
                self.initial_settings,
                run_binding=normalize_run_binding(
                    self.query_one("#settings-run-binding", Input).value
                ),
                confirm_mutations=self.query_one("#settings-confirm", Checkbox).value,
                keyboard_mode=self.query_one("#settings-keyboard", Checkbox).value,
                results_orientation=str(self.query_one("#settings-orientation", Select).value),
                horizontal_size=_result_size(
                    self.query_one("#settings-horizontal-size", Input).value, "Height"
                ),
                vertical_size=_result_size(
                    self.query_one("#settings-vertical-size", Input).value, "Width"
                ),
            )
            self.save_preferences(settings)
        except (SqlExplorerConfigurationError, ValueError, OSError) as exc:
            self.query_one("#settings-notice", Static).update(str(exc))
            return
        self.dismiss("connections" if change_connections else "saved")
