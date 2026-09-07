"""Shared first-launch and in-session connection-file picker."""

from __future__ import annotations

from pathlib import Path
from subprocess import SubprocessError
from threading import Event
from typing import ClassVar, Optional

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .connections import check_connections_file
from .discovery import DiscoveryProgress, discover_connections
from .inputs import EditableInput
from .styles import INTERACTION_CSS, explorer_css_variables


class ConnectionsPickerScreen(ModalScreen[Optional[Path]]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+l", "path", "Enter path", show=False),
    ]
    DEFAULT_CSS = """
    ConnectionsPickerScreen { align: center middle; }
    #connections-picker {
        width: 85%; max-width: 110; height: 80%; max-height: 36;
        border: solid $accent; background: $surface;
    }
    #connections-picker-title { height: 3; padding: 1 2; }
    #connections-options { height: 1fr; }
    #connections-progress { height: 3; padding: 0 2; }
    #connections-path { height: 3; margin: 0 1; }
    #connections-picker-help { height: 2; padding: 0 2; }
    """

    def __init__(
        self, current: Path | None = None, *, auto_select: bool = False, error: str = ""
    ) -> None:
        super().__init__()
        self.current = current
        self.auto_select = auto_select
        self.initial_error = error
        self.cancelled = Event()
        self.paths: tuple[Path, ...] = ()

    def compose(self) -> ComposeResult:
        with Vertical(id="connections-picker"):
            yield Static("Select a .connections file", id="connections-picker-title")
            yield OptionList(id="connections-options")
            yield Static(self.initial_error or "Searching local disks…", id="connections-progress")
            yield EditableInput(
                value=str(self.current) if self.current else "",
                placeholder="Enter a .connections path",
                id="connections-path",
            )
            yield Static(
                "↑/↓: choose · Enter: select · Ctrl+L: path · Esc: cancel",
                id="connections-picker-help",
            )

    def on_mount(self) -> None:
        if self.current is not None:
            self._show_progress(DiscoveryProgress((self.current,)))
        self.query_one(OptionList).focus()
        self._scan()

    @work(thread=True, exit_on_error=False)
    def _scan(self) -> None:
        try:
            for progress in discover_connections(self.cancelled):
                if self.cancelled.is_set():
                    return
                self.app.call_from_thread(self._show_progress, progress)
        except (OSError, ValueError, RuntimeError, SubprocessError) as exc:
            if not self.cancelled.is_set():
                self.app.call_from_thread(self._show_error, str(exc))

    def _show_progress(self, progress: DiscoveryProgress) -> None:
        paths = tuple(sorted(set(progress.paths) | ({self.current} if self.current else set())))
        options = self.query_one(OptionList)
        if paths != self.paths:
            selected = self.current
            if options.highlighted is not None and self.paths:
                selected = self.paths[options.highlighted]
            self.paths = paths
            options.clear_options()
            options.add_options(Option(Text(str(path)), id=str(path)) for path in paths)
            if selected in paths:
                options.highlighted = paths.index(selected)
            elif paths:
                options.highlighted = 0
        if self.initial_error:
            message = self.initial_error
        elif progress.complete:
            message = (
                f"Found {len(paths)} file(s). Select one or enter a path."
                if paths
                else "No .connections found near virtual environments. Enter a path."
            )
        else:
            message = (
                f"Searching local disks… {progress.directories:,} directories, "
                f"{len(paths)} file(s)."
            )
        self.query_one("#connections-progress", Static).update(Text(message))
        if progress.complete and self.auto_select and len(paths) == 1:
            self.auto_select = False
            self._choose(paths[0])

    def _show_error(self, message: str) -> None:
        self.auto_select = False
        self.initial_error = message
        self.query_one("#connections-progress", Static).update(Text(message))

    def _choose(self, path: Path) -> None:
        try:
            selected = check_connections_file(path)
        except (OSError, ValueError, RuntimeError) as exc:
            self._show_error(str(exc))
            return
        self.cancelled.set()
        self.dismiss(selected)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_id is not None:
            self._choose(Path(event.option_id))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._choose(Path(event.value))

    def action_path(self) -> None:
        self.query_one(Input).focus()

    def action_cancel(self) -> None:
        self.cancelled.set()
        self.dismiss(None)

    def on_unmount(self) -> None:
        self.cancelled.set()


class ConnectionsPickerApp(App[Optional[Path]]):
    CSS = INTERACTION_CSS

    def __init__(
        self, current: Path | None = None, *, auto_select: bool = False, error: str = ""
    ) -> None:
        super().__init__()
        self.picker = ConnectionsPickerScreen(current, auto_select=auto_select, error=error)

    def get_css_variables(self) -> dict[str, str]:
        return explorer_css_variables()

    def on_mount(self) -> None:
        self.push_screen(self.picker, self.exit)
