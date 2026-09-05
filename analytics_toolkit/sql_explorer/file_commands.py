"""Save and new-file commands for the SQL Explorer."""
# ruff: noqa: FBT003, PLR0913, SLF001

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional, cast

from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from .errors import SqlExplorerConfigurationError
from .inputs import EditableInput
from .widgets import FileNavigationScreen

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
        border: solid $accent;
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
            yield EditableInput(placeholder=self._placeholder, id="new-file-name")
            yield Static("", id="new-file-notice")
            with Horizontal(id="new-file-actions"):
                yield Button("Choose directory", id="new-file-confirm")
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
        if len(app.screen_stack) > 1:
            return
        workspace = app.active_workspace
        if workspace.current_file is None:
            app._start_new_sql_file(workspace, text=workspace.editor.text)
        else:
            app._save_workspace(workspace)

    def _save_workspace(self, workspace: Any) -> bool:
        app = cast("Any", self)
        path = workspace.current_file
        if path is None:
            return False
        if path.suffix.casefold() != ".sql" or not path.is_file():
            app.show_error(OSError("The opened SQL file no longer exists."), workspace)
            return False
        text = workspace.editor.text
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            app.show_error(exc, workspace)
            return False
        workspace.saved_text = text
        app._set_notice(f"Saved {path}", workspace)
        app._refresh_tab(workspace)
        app._update_status(workspace)
        return True

    def _command_save(self, arguments: list[str]) -> None:
        app = cast("Any", self)
        if arguments:
            app.show_error(SqlExplorerConfigurationError("Usage: save"))
            return
        app.action_save_file()

    def action_new_sql_file(self) -> None:
        app = cast("Any", self)
        if len(app.screen_stack) == 1:
            workspace = app.active_workspace
            app._start_new_sql_file(
                workspace,
                text="",
                new_tab=not workspace.is_clean_untitled,
            )

    def _start_new_sql_file(
        self,
        workspace: Any,
        *,
        text: str,
        after_create: Any = None,
        new_tab: bool = False,
    ) -> None:
        app = cast("Any", self)
        app.push_screen(
            NewSqlFileScreen(),
            lambda filename: app._new_sql_filename_selected(
                filename,
                workspace,
                text=text,
                after_create=after_create,
                new_tab=new_tab,
            ),
        )

    def _new_sql_filename_selected(
        self,
        filename: str | None,
        workspace: Any = None,
        *,
        text: str = "",
        after_create: Any = None,
        new_tab: bool = False,
    ) -> None:
        app = cast("Any", self)
        workspace = workspace or app.active_workspace
        if filename is None:
            if after_create is not None:
                after_create(False)
            return
        app.push_screen(
            FileNavigationScreen(Path.cwd(), select_directory=True),
            lambda directory: app._new_sql_directory_selected(
                filename,
                directory,
                workspace,
                text=text,
                after_create=after_create,
                new_tab=new_tab,
            ),
        )

    def _new_sql_directory_selected(
        self,
        filename: str,
        directory: Path | None,
        workspace: Any = None,
        *,
        text: str | None = None,
        after_create: Any = None,
        new_tab: bool = False,
    ) -> None:
        app = cast("Any", self)
        workspace = workspace or app.active_workspace
        if directory is None:
            if after_create is not None:
                after_create(False)
            return
        path = (directory / filename).resolve()
        try:
            path.relative_to(Path.cwd().resolve())
        except ValueError:
            error = SqlExplorerConfigurationError("Destination must remain in this project.")
            app.show_error(error, workspace)
            if after_create is not None:
                after_create(False)
            return
        if path.exists():
            app.show_error(
                SqlExplorerConfigurationError(f"File already exists: {path}"),
                workspace,
            )
            if after_create is not None:
                after_create(False)
            return
        file_text = (
            workspace.editor.text
            if text is None and workspace.current_file is not None
            else text or ""
        )
        app._create_sql_file(
            path,
            text=file_text,
            workspace=workspace,
            after_create=after_create,
            new_tab=new_tab,
        )

    def _create_sql_file(
        self,
        path: Path,
        *,
        text: str = "",
        workspace: Any = None,
        after_create: Any = None,
        new_tab: bool = False,
    ) -> bool:
        app = cast("Any", self)
        workspace = workspace or app.active_workspace
        try:
            with path.open("x", encoding="utf-8") as file:
                file.write(text)
        except OSError as exc:
            app.show_error(exc, workspace)
            if after_create is not None:
                after_create(False)
            return False
        target = app._add_workspace() if new_tab else workspace
        if target.is_mounted:
            app._finish_created_sql_file(target, path, text, after_create)
        else:
            target.pending_mount_action = lambda: app._finish_created_sql_file(
                target,
                path,
                text,
                after_create,
            )
        return True

    def _finish_created_sql_file(
        self,
        workspace: Any,
        path: Path,
        text: str,
        after_create: Any,
    ) -> None:
        app = cast("Any", self)
        editor = workspace.editor
        if editor.text != text:
            editor.text = text
            editor.cursor_location = (0, 0)
        workspace.current_file = path
        workspace.saved_text = text
        app._activate_tab(workspace.tab_id)
        app._set_notice(f"Created {path}", workspace)
        app._refresh_tab(workspace)
        app._update_status(workspace)
        editor.focus()
        if after_create is not None:
            after_create(True)
