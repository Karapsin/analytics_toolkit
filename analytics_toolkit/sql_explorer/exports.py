"""Result export commands for the SQL Explorer."""

# ruff: noqa: SLF001

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

from textual import work
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .errors import SqlExplorerConfigurationError
from .file_commands import NewFileScreen
from .widgets import ConfirmMutationScreen, FileNavigationScreen

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from .app import SqlExplorerApp

ExportFormat = Literal["csv", "excel"]


class ConfirmExportScreen(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left", "select_confirm", "Select confirm", show=False, priority=True),
        Binding("right", "select_cancel", "Select cancel", show=False, priority=True),
        Binding("y", "confirm", "Confirm", show=False),
        Binding("n,escape", "cancel", "Cancel", show=False),
    ]

    CSS = """
    ConfirmExportScreen {
        align: center middle;
    }
    #export-confirmation-dialog {
        width: 70%;
        max-width: 80;
        height: auto;
        border: solid $warning;
        background: $panel;
        padding: 1 2;
    }
    #export-confirmation-buttons {
        height: 3;
        align-horizontal: center;
    }
    """

    def __init__(self, message: str, *, confirm_label: str) -> None:
        super().__init__()
        self.message = message
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="export-confirmation-dialog"):
            yield Static(self.message, markup=False)
            with Horizontal(id="export-confirmation-buttons"):
                yield Button(
                    f"{self.confirm_label} [Y]",
                    id="export-confirm",
                )
                yield Button("Cancel [N]", id="export-cancel")

    def action_confirm(self) -> None:
        self.dismiss(result=True)

    def action_cancel(self) -> None:
        self.dismiss(result=False)

    def action_select_confirm(self) -> None:
        self.query_one("#export-confirm", Button).focus()

    def action_select_cancel(self) -> None:
        self.query_one("#export-cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "export-confirm")


class SqlExplorerExportCommandsMixin:
    """Select a destination and export the current Explorer query result."""

    def _command_to_excel(self, arguments: list[str]) -> None:
        self._start_export_command("excel", arguments)

    def _command_to_csv(self, arguments: list[str]) -> None:
        self._start_export_command("csv", arguments)

    def _start_export_command(
        self,
        output_format: ExportFormat,
        arguments: list[str],
    ) -> None:
        app = cast("SqlExplorerApp", self)
        workspace = app.active_workspace
        command = "to_excel" if output_format == "excel" else "to_csv"
        if arguments:
            app.show_error(SqlExplorerConfigurationError(f"Usage: {command}"))
            return
        connection_key = workspace.session.database.connection_key
        if app._database_is_busy(connection_key):
            app._set_notice(
                f"Wait for the current SQL operation on {connection_key} before exporting.",
                workspace,
            )
            return
        table = workspace.result_table
        if not workspace.results_open or table.styles.display == "none":
            message = "Run a row-producing query before exporting."
            app.show_error(SqlExplorerConfigurationError(message))
            return
        try:
            state = workspace.session.export_state()
        except Exception as exc:  # noqa: BLE001 -- render Explorer errors in the result area.
            app.show_error(exc)
            return
        if state.truncated:
            app.push_screen(
                ConfirmExportScreen(
                    "Save all query result rows? This may run the query again.",
                    confirm_label="Save all",
                ),
                lambda confirmed: self._full_export_confirmed(
                    output_format,
                    confirmed,
                    workspace.tab_id,
                ),
            )
            return
        self._choose_export_filename(output_format, workspace.tab_id)

    def _full_export_confirmed(
        self,
        output_format: ExportFormat,
        confirmed: object,
        tab_id: str | None = None,
    ) -> None:
        app = cast("SqlExplorerApp", self)
        workspace = app._workspaces.get(tab_id or app._active_tab_id)
        if workspace is None:
            return
        if confirmed is not True:
            app._set_notice("Export cancelled.", workspace)
            return
        state = workspace.session.export_state()
        if (
            state.dataframe is None
            and state.plan.requires_confirmation
            and workspace.session.settings.confirm_mutations
        ):
            database = getattr(state, "database", None) or workspace.session.database
            plan = replace(
                state.plan,
                execution_sql=state.plan.full_execution_sql,
                server_limited=False,
            )
            app.push_screen(
                ConfirmMutationScreen(
                    plan,
                    db_key=database.connection_key,
                    backend=database.backend,
                ),
                lambda confirmed: (
                    self._choose_export_filename(output_format, workspace.tab_id)
                    if confirmed
                    else app._set_notice("Export cancelled.", workspace)
                ),
            )
            return
        self._choose_export_filename(output_format, workspace.tab_id)

    def _choose_export_filename(
        self,
        output_format: ExportFormat,
        tab_id: str | None = None,
    ) -> None:
        app = cast("SqlExplorerApp", self)
        suffix = ".xlsx" if output_format == "excel" else ".csv"
        app.push_screen(
            NewFileScreen(
                suffix=suffix,
                title=f"Export result filename ({suffix} required)",
                placeholder=f"query_result{suffix}",
            ),
            lambda filename: self._export_filename_selected(output_format, filename, tab_id),
        )

    def _export_filename_selected(
        self,
        output_format: ExportFormat,
        filename: str | None,
        tab_id: str | None = None,
    ) -> None:
        app = cast("SqlExplorerApp", self)
        if filename is not None:
            app.push_screen(
                FileNavigationScreen(Path.cwd(), select_directory=True),
                lambda directory: self._export_directory_selected(
                    output_format,
                    filename,
                    directory,
                    tab_id,
                ),
            )

    def _export_directory_selected(
        self,
        output_format: ExportFormat,
        filename: str,
        directory: Path | None,
        tab_id: str | None = None,
    ) -> None:
        app = cast("SqlExplorerApp", self)
        workspace = app._workspaces.get(tab_id or app._active_tab_id)
        if workspace is None:
            return
        if directory is None:
            return
        path = (directory / filename).resolve()
        try:
            path.relative_to(Path.cwd().resolve())
        except ValueError:
            message = "Destination must remain in this project."
            app.show_error(SqlExplorerConfigurationError(message), workspace)
            return
        if path.exists():
            if not path.is_file():
                message = f"Destination is not a file: {path}"
                app.show_error(SqlExplorerConfigurationError(message), workspace)
                return
            app.push_screen(
                ConfirmExportScreen(
                    f"Replace existing file {path}?",
                    confirm_label="Replace",
                ),
                lambda confirmed: (
                    self._write_export(output_format, path, workspace.tab_id)
                    if confirmed
                    else app._set_notice("Export cancelled.", workspace)
                ),
            )
            return
        self._write_export(output_format, path, workspace.tab_id)

    def _write_export(
        self,
        output_format: ExportFormat,
        path: Path,
        tab_id: str | None = None,
    ) -> None:
        app = cast("SqlExplorerApp", self)
        workspace = app._workspaces.get(tab_id or app._active_tab_id)
        if workspace is None:
            return
        connection_key = workspace.session.database.connection_key
        if app._database_is_busy(connection_key):
            app._set_notice(
                f"Wait for the current SQL operation on {connection_key} before exporting.",
                workspace,
            )
            return
        workspace.busy = True
        workspace.cancelling = False
        workspace.query_state = "running"
        workspace.operation_database = workspace.session.database
        app._set_notice(f"Exporting query result to {path}...", workspace)
        app._update_status(workspace)
        self._export_in_worker(output_format, path, workspace.tab_id, workspace.session)

    @work(thread=True, group="sql-explorer-export", exclusive=False, exit_on_error=False)
    def _export_in_worker(
        self,
        output_format: ExportFormat,
        path: Path,
        tab_id: str,
        session: Any,
    ) -> None:
        app = cast("SqlExplorerApp", self)
        try:
            dataframe = session.export_dataframe()
            if output_format == "excel":
                dataframe.to_excel(path, index=False)
            else:
                dataframe.to_csv(path, index=False)
        except Exception as exc:  # noqa: BLE001 -- worker failures are rendered in the TUI.
            app.call_from_thread(self._finish_export_error, exc, tab_id)
        else:
            app.call_from_thread(self._finish_export, path, tab_id)

    def _finish_export(self, path: Path, tab_id: str | None = None) -> None:
        app = cast("SqlExplorerApp", self)
        workspace = app._workspaces.get(tab_id or app._active_tab_id)
        if workspace is None:
            return
        workspace.reset_query_state()
        if workspace.closing:
            app._remove_workspace(workspace.tab_id)
            app._finish_exit_if_ready()
            return
        app._update_status(workspace)
        app._set_notice(f"Saved query result to {path}.", workspace)
        app._drain_query_queue()

    def _finish_export_error(self, exc: Exception, tab_id: str | None = None) -> None:
        app = cast("SqlExplorerApp", self)
        workspace = app._workspaces.get(tab_id or app._active_tab_id)
        if workspace is None:
            return
        workspace.reset_query_state()
        if workspace.closing:
            app._remove_workspace(workspace.tab_id)
            app._finish_exit_if_ready()
            return
        app._update_status(workspace)
        app.show_error(exc, workspace)
        app._drain_query_queue()


__all__ = ["ConfirmExportScreen", "SqlExplorerExportCommandsMixin"]
