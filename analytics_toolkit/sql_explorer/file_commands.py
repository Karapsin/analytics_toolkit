"""Save and new-file commands for the SQL Explorer."""
# ruff: noqa: SLF001

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional, cast

from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from .editor import SqlEditor
from .errors import SqlExplorerConfigurationError
from .widgets import DiscardChangesScreen, FileNavigationScreen

if TYPE_CHECKING:
    from textual.app import ComposeResult


class NewFileScreen(ModalScreen[Optional[str]]):
    """Collect a safe filename before choosing its destination."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("enter", "confirm", "Choose directory", show=False, priority=True),
    ]

    CSS = """
    NewFileScreen {
        align: center middle;
    }
    #new-file-dialog {
        width: 70%;
        max-width: 80;
        height: auto;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #new-file-actions {
        height: 3;
        align-horizontal: right;
    }
    #new-file-notice {
        height: 1;
    }
    """

    def __init__(self, *, suffix: str, title: str, placeholder: str) -> None:
        super().__init__()
        self.suffix = suffix
        self._dialog_title = title
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="new-file-dialog"):
            yield Static(self._dialog_title, markup=False)
            yield Input(placeholder=self._placeholder, id="new-file-name")
            yield Static("", id="new-file-notice")
            with Horizontal(id="new-file-actions"):
                yield Button("Choose directory", variant="primary", id="new-file-confirm")
                yield Button("Cancel", id="new-file-cancel")

    def on_mount(self) -> None:
        self.query_one("#new-file-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "new-file-name":
            self.action_confirm()

    def action_confirm(self) -> None:
        value = self.query_one("#new-file-name", Input).value.strip()
        if not self._is_filename(value):
            self.query_one("#new-file-notice", Static).update(
                f"Enter one filename ending in {self.suffix}."
            )
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-file-confirm":
            self.action_confirm()
        elif event.button.id == "new-file-cancel":
            self.action_cancel()

    def _is_filename(self, value: str) -> bool:
        return (
            bool(value)
            and Path(value).name == value
            and value not in {".", ".."}
            and Path(value).suffix.casefold() == self.suffix.casefold()
        )


class NewSqlFileScreen(NewFileScreen):
    """Collect a safe SQL filename before choosing its destination."""

    def __init__(self) -> None:
        super().__init__(
            suffix=".sql",
            title="New SQL file name (.sql required)",
            placeholder="query.sql",
        )


class SqlExplorerFileCommandsMixin:
    """Persist an opened SQL file or create a blank SQL file safely."""

    def action_save_file(self) -> None:
        app = cast("Any", self)
        path = app._current_file
        if path is None:
            app._set_notice("Open an existing .sql file before saving.")
            return
        if path.suffix.casefold() != ".sql" or not path.is_file():
            app.show_error(OSError("The opened SQL file no longer exists."))
            return
        try:
            path.write_text(app.query_one(SqlEditor).text, encoding="utf-8")
        except OSError as exc:
            app.show_error(exc)
            return
        app._saved_text = app.query_one(SqlEditor).text
        app._set_notice(f"Saved {path}")
        app._update_status()

    def _command_save(self, arguments: list[str]) -> None:
        app = cast("Any", self)
        if arguments:
            app.show_error(SqlExplorerConfigurationError("Usage: save"))
            return
        app.action_save_file()

    def action_new_sql_file(self) -> None:
        app = cast("Any", self)
        if len(app.screen_stack) == 1:
            app.push_screen(NewSqlFileScreen(), app._new_sql_filename_selected)

    def _new_sql_filename_selected(self, filename: str | None) -> None:
        app = cast("Any", self)
        if filename is not None:
            app.push_screen(
                FileNavigationScreen(Path.cwd(), select_directory=True),
                lambda directory: app._new_sql_directory_selected(filename, directory),
            )

    def _new_sql_directory_selected(self, filename: str, directory: Path | None) -> None:
        app = cast("Any", self)
        if directory is None:
            return
        path = (directory / filename).resolve()
        try:
            path.relative_to(Path.cwd().resolve())
        except ValueError:
            error = SqlExplorerConfigurationError("Destination must remain in this project.")
            app.show_error(error)
            return
        if path.exists():
            app.show_error(SqlExplorerConfigurationError(f"File already exists: {path}"))
            return
        editor = app.query_one(SqlEditor)
        if editor.text != app._saved_text:
            app.push_screen(
                DiscardChangesScreen(path),
                lambda discard: app._create_sql_file(path) if discard else None,
            )
            return
        text = editor.text if app._current_file is not None and editor.text else ""
        app._create_sql_file(path, text=text)

    def _create_sql_file(self, path: Path, *, text: str = "") -> None:
        app = cast("Any", self)
        try:
            with path.open("x", encoding="utf-8") as file:
                file.write(text)
        except OSError as exc:
            app.show_error(exc)
            return
        editor = app.query_one(SqlEditor)
        editor.text = text
        editor.cursor_location = (0, 0)
        app._current_file = path
        app._saved_text = text
        app._set_notice(f"Created {path}")
        app._update_status()
        editor.focus()
