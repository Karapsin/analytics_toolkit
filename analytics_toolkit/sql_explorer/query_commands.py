"""Cross-tab user-query scheduling, cancellation, and exit coordination."""

# ruff: noqa: FBT001, SLF001

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from textual import work

from .create_table import CreateTablePlan
from .errors import SqlExplorerConfigurationError
from .runtime import DatabaseSelection
from .tabs import SaveChangesScreen
from .widgets import ConfirmMutationScreen

if TYPE_CHECKING:
    from .runtime import ExplorerCancelResult, ExplorerRunResult, ExplorerSession
    from .scheduling import ExplorerQueryJob
    from .statements import ExplorerExecutionPlan
    from .workspace import SqlExplorerWorkspace


class SqlExplorerQueryCommandsMixin:
    """Run immutable tab snapshots through one FIFO queue per database."""

    def action_run_query(self) -> None:
        app = cast("Any", self)
        if len(app.screen_stack) > 1:
            return
        workspace = app.active_workspace
        if workspace.busy and workspace.running_job_id is None:
            app._set_notice("A SQL operation is already running.", workspace)
            return
        if app._query_scheduler.job_for_tab(workspace.tab_id) is not None:
            app._set_notice("This tab already has a queued or running query.", workspace)
            return
        sql_text = workspace.editor.execution_text()
        try:
            plan = workspace.session.plan(sql_text)
        except Exception as exc:  # noqa: BLE001 -- errors are rendered in the TUI.
            app.show_error(exc, workspace)
            return
        database = DatabaseSelection(
            workspace.session.database.connection_key,
            workspace.session.database.backend,
        )
        if plan.requires_confirmation and workspace.session.settings.confirm_mutations:
            app.push_screen(
                ConfirmMutationScreen(
                    plan,
                    db_key=database.connection_key,
                    backend=database.backend,
                ),
                lambda confirmed: (
                    app._enqueue_query(workspace, plan, database) if confirmed else None
                ),
            )
            return
        app._enqueue_query(workspace, plan, database)

    def _start_execution(self, plan: ExplorerExecutionPlan) -> None:
        """Compatibility entrypoint used by tests and confirmation callbacks."""
        app = cast("Any", self)
        workspace = app.active_workspace
        database = DatabaseSelection(
            workspace.session.database.connection_key,
            workspace.session.database.backend,
        )
        app._enqueue_query(workspace, plan, database)

    def _enqueue_query(
        self,
        workspace: SqlExplorerWorkspace,
        plan: ExplorerExecutionPlan,
        database: DatabaseSelection,
    ) -> None:
        app = cast("Any", self)
        job = app._query_scheduler.enqueue(workspace.tab_id, plan, database)
        if job is None:
            app._set_notice("This tab already has a queued or running query.", workspace)
            return
        app.close_results(focus_editor=False, workspace=workspace)
        workspace.query_state = "queued"
        position = app._query_scheduler.position(workspace.tab_id)
        app._set_notice(f"Query queued (position {position or 1}).", workspace)
        app._update_status(workspace)
        app._drain_query_queue()

    def _drain_query_queue(self) -> None:
        app = cast("Any", self)
        blocked_databases = {
            (workspace.operation_database or workspace.session.database).connection_key
            for workspace in app._workspaces.values()
            if workspace.busy and workspace.running_job_id is None
        }
        for job in app._query_scheduler.take_startable(blocked_databases):
            workspace = app._workspaces.get(job.tab_id)
            if workspace is None:
                app._query_scheduler.complete(job.job_id)
                continue
            workspace.busy = True
            workspace.cancelling = False
            workspace.query_state = "running"
            workspace.running_job_id = job.job_id
            workspace.operation_database = job.database
            app._set_notice(None, workspace)
            app._update_status(workspace)
            app._execute_in_worker(job, workspace.session)

    def _database_is_busy(self, connection_key: str) -> bool:
        app = cast("Any", self)
        normalized = connection_key.casefold()
        if app._query_scheduler.is_database_active(normalized):
            return True
        return any(
            workspace.busy
            and (
                workspace.operation_database or workspace.session.database
            ).connection_key.casefold()
            == normalized
            for workspace in app._workspaces.values()
        )

    @work(thread=True, group="sql-explorer", exclusive=False, exit_on_error=False)
    def _execute_in_worker(
        self,
        job: ExplorerQueryJob,
        session: ExplorerSession,
    ) -> None:
        app = cast("Any", self)
        try:
            result = session.execute(job.plan, database=job.database)
        except Exception as exc:  # noqa: BLE001 -- worker failures are rendered in the TUI.
            app.call_from_thread(app._finish_query_job, job, None, exc)
        else:
            app.call_from_thread(app._finish_query_job, job, result, None)

    @work(thread=True, group="sql-explorer-cancel", exclusive=False, exit_on_error=False)
    def _cancel_in_worker(self, tab_id: str, session: ExplorerSession) -> None:
        app = cast("Any", self)
        try:
            result = session.cancel_active()
        except Exception as exc:  # noqa: BLE001 -- errors are rendered in the TUI.
            app.call_from_thread(app._finish_cancel_error_for, tab_id, exc)
        else:
            app.call_from_thread(app._finish_cancel_for, tab_id, result)

    def _finish_query_job(
        self,
        job: ExplorerQueryJob,
        result: ExplorerRunResult | None,
        error: Exception | None,
    ) -> None:
        app = cast("Any", self)
        if app._query_scheduler.complete(job.job_id) is None:
            return
        if isinstance(job.plan, CreateTablePlan):
            coordinator = app._completion_pool.coordinator_for(job.database.connection_key)
            if coordinator is not None:
                coordinator.invalidate_tables()
            for owner in app._workspaces.values():
                if owner.session.database.connection_key == job.database.connection_key:
                    app._cancel_completion_request(owner)
                    owner.completion_context = None
                    owner.completion_candidates = ()
                    owner.completion_menu.styles.display = "none"
        workspace = app._workspaces.get(job.tab_id)
        if workspace is not None:
            workspace.reset_query_state()
            if workspace.closing:
                app._remove_workspace(workspace.tab_id)
            elif error is not None:
                app.show_error(error, workspace)
            elif result is not None:
                app._render_result(result, workspace)
        app._drain_query_queue()
        app._finish_exit_if_ready()

    def _render_result(
        self,
        result: ExplorerRunResult,
        workspace: SqlExplorerWorkspace,
    ) -> None:
        app = cast("Any", self)
        workspace.last_run_result = result
        app._set_notice(None, workspace)
        if result.dataframe is None:
            app.close_results(focus_editor=False, workspace=workspace)
            app._set_notice(result.status, workspace)
        else:
            app.show_dataframe(result.dataframe, workspace)
        app._update_status(workspace)

    def _finish_result(self, result: ExplorerRunResult) -> None:
        app = cast("Any", self)
        workspace = app.active_workspace
        exit_after = workspace.exit_after_cancel
        workspace.reset_query_state()
        app._update_status(workspace)
        if exit_after:
            app.exit()
            return
        app._render_result(result, workspace)

    def _finish_error(self, exc: Exception) -> None:
        app = cast("Any", self)
        workspace = app.active_workspace
        exit_after = workspace.exit_after_cancel
        workspace.reset_query_state()
        app._update_status(workspace)
        if exit_after:
            app.exit()
            return
        app.show_error(exc, workspace)

    def _finish_cancel(self, result: ExplorerCancelResult) -> None:
        app = cast("Any", self)
        app._finish_cancel_for(app._active_tab_id, result)

    def _finish_cancel_error(self, exc: Exception) -> None:
        app = cast("Any", self)
        app._finish_cancel_error_for(app._active_tab_id, exc)

    def _command_exit_force(self, arguments: list[str]) -> None:
        app = cast("Any", self)
        if arguments:
            app.show_error(SqlExplorerConfigurationError("Usage: exit! or q!"))
            return
        app._request_exit(mode="discard")

    def _command_write_quit(self, arguments: list[str]) -> None:
        app = cast("Any", self)
        if arguments:
            app.show_error(SqlExplorerConfigurationError("Usage: wq"))
            return
        app._request_exit(mode="save")

    def _request_exit(self, *, mode: str = "ask") -> None:
        app = cast("Any", self)
        if len(app.screen_stack) > 1 or app._exit_requested:
            return
        app._exit_save_all = mode == "save"
        if mode == "discard":
            app._exit_dirty_tabs.clear()
            app._begin_exit_shutdown()
            return
        app._exit_dirty_tabs = [
            tab_id for tab_id in app._tab_order if app._workspaces[tab_id].is_dirty
        ]
        app._prompt_next_exit_dirty_tab()

    async def action_quit(self) -> None:
        """Route framework-level quit actions through multi-tab protection."""
        self._request_exit()

    def _prompt_next_exit_dirty_tab(self) -> None:
        app = cast("Any", self)
        while app._exit_dirty_tabs:
            tab_id = app._exit_dirty_tabs[0]
            workspace = app._workspaces.get(tab_id)
            if workspace is None or not workspace.is_dirty:
                app._exit_dirty_tabs.pop(0)
                continue
            app._activate_tab(tab_id)
            if app._exit_save_all:
                app._exit_dirty_decision(tab_id, "save")
                return
            app.push_screen(
                SaveChangesScreen(workspace.file_label),
                lambda decision, owner=tab_id: app._exit_dirty_decision(owner, decision),
            )
            return
        app._begin_exit_shutdown()

    def _exit_dirty_decision(self, tab_id: str, decision: object) -> None:
        app = cast("Any", self)
        workspace = app._workspaces.get(tab_id)
        if workspace is None:
            app._prompt_next_exit_dirty_tab()
            return
        if decision == "cancel":
            app._exit_dirty_tabs.clear()
            return
        if decision == "save":
            if workspace.current_file is not None:
                if not app._save_workspace(workspace):
                    app._exit_dirty_tabs.clear()
                    return
                app._exit_dirty_tabs.pop(0)
                app._prompt_next_exit_dirty_tab()
            else:
                app._start_new_sql_file(
                    workspace,
                    text=workspace.editor.text,
                    after_create=lambda created: app._exit_save_finished(tab_id, created),
                )
            return
        app._exit_dirty_tabs.pop(0)
        app._prompt_next_exit_dirty_tab()

    def _exit_save_finished(self, tab_id: str, created: bool) -> None:
        app = cast("Any", self)
        if not created:
            app._exit_dirty_tabs.clear()
            return
        if app._exit_dirty_tabs and app._exit_dirty_tabs[0] == tab_id:
            app._exit_dirty_tabs.pop(0)
        app._prompt_next_exit_dirty_tab()

    def _begin_exit_shutdown(self) -> None:
        app = cast("Any", self)
        app._exit_requested = True
        for workspace in tuple(app._workspaces.values()):
            if app._query_scheduler.remove_pending_tab(workspace.tab_id) is not None:
                workspace.reset_query_state()
            if workspace.busy:
                app._request_cancel(workspace=workspace, exit_after=True)
        app._finish_exit_if_ready()

    def _finish_exit_if_ready(self) -> None:
        app = cast("Any", self)
        if (
            app._exit_requested
            and app._query_scheduler.active_count == 0
            and app._query_scheduler.pending_count == 0
            and not any(workspace.busy for workspace in app._workspaces.values())
        ):
            app.exit()

    def _request_cancel(
        self,
        *,
        workspace: SqlExplorerWorkspace | None = None,
        exit_after: bool = False,
        close_after: bool = False,
    ) -> None:
        app = cast("Any", self)
        workspace = workspace or app.active_workspace
        pending = app._query_scheduler.remove_pending_tab(workspace.tab_id)
        if pending is not None:
            workspace.reset_query_state()
            app._set_notice("Removed queued query.", workspace)
            app._update_status(workspace)
            if close_after:
                app._remove_workspace(workspace.tab_id)
            app._drain_query_queue()
            app._finish_exit_if_ready()
            return
        if not workspace.busy:
            if exit_after:
                if app._exit_requested:
                    app._finish_exit_if_ready()
                else:
                    app.exit()
            elif close_after:
                app._remove_workspace(workspace.tab_id)
            else:
                app._set_notice("No SQL operation is running.", workspace)
            return
        if workspace.cancelling:
            workspace.exit_after_cancel = workspace.exit_after_cancel or exit_after
            workspace.closing = workspace.closing or close_after
            app._set_notice("Cancellation is already in progress.", workspace)
            return
        workspace.cancelling = True
        workspace.query_state = "cancelling"
        workspace.exit_after_cancel = exit_after
        workspace.closing = close_after
        app._set_notice("Cancelling the active explorer query...", workspace)
        app._update_status(workspace)
        app._cancel_in_worker(workspace.tab_id, workspace.session)

    def _finish_cancel_for(
        self,
        tab_id: str,
        result: ExplorerCancelResult,
    ) -> None:
        app = cast("Any", self)
        workspace = app._workspaces.get(tab_id)
        if workspace is None:
            return
        app._set_notice(result.status, workspace)
        app._update_status(workspace)

    def _finish_cancel_error_for(self, tab_id: str, exc: Exception) -> None:
        app = cast("Any", self)
        workspace = app._workspaces.get(tab_id)
        if workspace is None:
            return
        workspace.cancelling = False
        workspace.query_state = "running" if workspace.busy else "ready"
        workspace.exit_after_cancel = False
        workspace.closing = False
        app._exit_requested = False
        app._update_status(workspace)
        app.show_error(exc, workspace)


__all__ = ["SqlExplorerQueryCommandsMixin"]
