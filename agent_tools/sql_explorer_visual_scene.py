#!/usr/bin/env python3
"""Render deterministic SQL Explorer scenes for headless current-host visual review."""

# ruff: noqa: EM102, FLY002, PLR2004, TRY003

from __future__ import annotations

import argparse
import json
import os
from copy import copy
from dataclasses import replace
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pandas as pd
from agent_tools.sql_explorer_lifecycle_trace import RulerLifecycleTrace
from agent_tools.sql_explorer_render_diagnostics import (
    SELECTOR_SCENES,
    rendered_rows_equal,
    ruler_lifecycle_snapshot,
    ruler_strip_observation,
    selector_readiness_assertions,
    selector_readiness_snapshot,
    strip_record,
    styled_content_row,
)
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.connections import ConnectionsRestart
from analytics_toolkit.sql_explorer.connections_picker import (
    ConnectionsPickerApp,
    ConnectionsPickerScreen,
)
from analytics_toolkit.sql_explorer.create_table_screen import CreateTableScreen
from analytics_toolkit.sql_explorer.discovery import DiscoveryProgress
from analytics_toolkit.sql_explorer.exports import ConfirmExportScreen
from analytics_toolkit.sql_explorer.file_commands import NewSqlFileScreen
from analytics_toolkit.sql_explorer.journal import QueryJournal
from analytics_toolkit.sql_explorer.journal_screen import JournalScreen
from analytics_toolkit.sql_explorer.picker import DatabasePickerApp
from analytics_toolkit.sql_explorer.runtime import (
    ExplorerCancelResult,
    ExplorerQueryState,
    ExplorerRunResult,
)
from analytics_toolkit.sql_explorer.settings import ExplorerSettings
from analytics_toolkit.sql_explorer.settings_screen import SettingsScreen
from analytics_toolkit.sql_explorer.statements import ExecutionRoute, build_execution_plan
from analytics_toolkit.sql_explorer.tabs import SaveChangesScreen, TabSelectButton
from analytics_toolkit.sql_explorer.widgets import (
    ConfirmMutationScreen,
    DiscardChangesScreen,
    FileNavigationScreen,
    ResultTable,
)
from rich.text import Text
from textual.app import ScreenStackError
from textual.color import Color
from textual.css.query import NoMatches
from textual.widgets import Button, Collapsible, Input, OptionList, Static

if TYPE_CHECKING:
    from textual.app import App
    from textual.widget import Widget


class VisualSession:
    """Database-free session fixture with the runtime surface used by the app."""

    def __init__(self, key: str = "gp") -> None:
        self.settings = ExplorerSettings()
        self.settings_warning: str | None = None
        self.database = SimpleNamespace(connection_key=key, backend="gp")
        self.active_query: ExplorerQueryState | None = None
        self.last_query: ExplorerQueryState | None = None

    def fork(self) -> VisualSession:
        child = copy(self)
        child.database = copy(self.database)
        child.active_query = None
        child.last_query = None
        return child

    def plan(self, sql_text: str) -> Any:
        return build_execution_plan(sql_text, self.database.backend)

    def execute(self, plan: Any, *, database: Any = None) -> ExplorerRunResult:
        del database
        return ExplorerRunResult(
            route=plan.route,
            dataframe=pd.DataFrame({"order_id": [101, 102], "revenue": [49.9, 125.0]}),
            displayed_rows=2,
            total_rows=2,
            truncated=False,
            status="Returned 2 rows.",
        )

    def switch_database(self, key: str) -> Any:
        self.database = SimpleNamespace(connection_key=key, backend="gp")
        return self.database

    def set_run_binding(self, value: str) -> ExplorerSettings:
        self.settings = replace(self.settings, run_binding=value)
        return self.settings

    def set_confirmation(self, *, enabled: bool) -> ExplorerSettings:
        self.settings = replace(self.settings, confirm_mutations=enabled)
        return self.settings

    def cancel_active(self) -> ExplorerCancelResult:
        return ExplorerCancelResult(1, 1, "Cancellation requested for 1 query.")


class VisualExplorerApp(SqlExplorerApp):
    def __init__(self, scene_id: str, evidence_path: Path, manifest_path: Path) -> None:
        self.visual_scene_id = scene_id
        self.visual_evidence_path = evidence_path
        self.visual_manifest_path = manifest_path
        self.creation_scene_ready = not (
            scene_id.startswith(("create-table", "journal-", "completion"))
            or scene_id
            in {
                "command-completion",
                "database-completion",
                "directory-armed",
                "save-changes",
                "save-changes-cancel",
                "tab-database-change",
            }
        )
        self.scene_configured = self.creation_scene_ready
        self._pending_scene_screen: (
            SaveChangesScreen | CreateTableScreen | FileNavigationScreen | None
        ) = None
        self._directory_scene_initialized = False
        self._visual_database_switch_started = False
        self._visual_workspace_restore_pending = False
        self._visual_database_restore_pending = False
        super().__init__(VisualSession())
        self.ruler_trace: RulerLifecycleTrace | None = None
        if scene_id == "tab-database-change":
            self.ruler_trace = RulerLifecycleTrace(self)
            self.ruler_trace.install()

    def on_unmount(self) -> None:
        if self.ruler_trace is not None:
            self.ruler_trace.close()
        super().on_unmount()

    def on_mount(self) -> None:
        super().on_mount()
        self.call_after_refresh(self._configure_visual_scene)

    async def _configure_visual_scene(self) -> None:
        workspace = self.active_workspace
        workspace.editor.text = "\n".join(
            (
                "SELECT order_id, customer_name, revenue",
                "FROM analytics.daily_orders",
                "WHERE order_date >= CURRENT_DATE - INTERVAL '7 days';",
            )
        )
        scene = self.visual_scene_id
        if scene.startswith("journal-"):
            self._configure_journal_scene(scene)
        elif scene.startswith("create-table"):
            await self._open_creation_scene(scene)
        elif scene.startswith("completion"):
            self._configure_completion_scene(scene)
        elif scene in {"command-completion", "database-completion"}:
            self._configure_command_scene(scene)
        elif scene in {"tab-database-change", "formatted-sql", "tabs-overflow", "find-replace"}:
            self._configure_document_scene(scene)
        elif scene in {"query-running", "result-success", "result-message"}:
            self._configure_results_scene(scene)
        elif scene in {
            "file-open",
            "directory-armed",
            "new-file",
            "mutation-confirm",
            "discard-confirm",
            "connections-switch-save",
            "save-changes",
            "save-changes-cancel",
            "export-confirm",
            "settings",
            "settings-invalid",
        }:
            await self._configure_modal_scene(scene)
        elif scene in {
            "keyboard-multicursor",
            "editor-command-caret",
            "column-ruler-scrolled",
            "results-vertical",
            "results-resized",
            "help-shortcuts",
            "help-movement",
            "help-movement-end",
        }:
            self._configure_workspace_scene(scene)
        elif scene != "editor-ready":
            raise ValueError(f"unsupported SQL Explorer visual scene: {scene}")

        self.call_after_refresh(self._refresh_visual_evidence)
        self.set_interval(0.2, self._refresh_visual_evidence)

    def _configure_journal_scene(self, scene: str) -> None:
        workspace = self.active_workspace
        if scene.startswith("journal-"):
            journal = QueryJournal(self.visual_evidence_path.parent / scene)
            if scene == "journal-populated":
                with journal.action(
                    "gp",
                    "gp",
                    "user",
                    user_sql=workspace.editor.text,
                    source_file="daily_orders.sql",
                ) as action, action.submission(workspace.editor.text + "\nLIMIT 201"):
                    pass
            elif scene == "journal-error":
                path = journal.store("gp").path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"invalid SQLite database")
            self.push_screen(JournalScreen(journal, "gp"))
            self.set_timer(0.6, self._mark_scene_ready)

    def _configure_command_scene(self, scene: str) -> None:
        workspace = self.active_workspace
        if scene in {"command-completion", "database-completion"}:
            command = workspace.command_input
            command.value = "db wa" if scene == "database-completion" else "c"
            command.cursor_position = len(command.value)
            command.database_keys = ("warehouse", "warehouse_dev")
            command.focus()

            def show_commands() -> None:
                command.refresh_completion()
                self.call_after_refresh(self._mark_scene_ready)

            self.set_timer(0.3, show_commands)

    def _configure_document_scene(self, scene: str) -> None:
        workspace = self.active_workspace
        if scene == "tab-database-change":
            target = self._add_workspace()
            previous_action = target.pending_mount_action

            def mounted() -> None:
                # The workspace consumes this hook after _finish_added_workspace.
                if previous_action is not None:
                    previous_action()
                self._visual_workspace_restore_pending = True

            target.pending_mount_action = mounted
        elif scene == "formatted-sql":
            workspace.editor.text = (
                "select order_id, revenue from analytics.daily_orders where revenue > 100;\n"
                "select count(*) from analytics.daily_orders;"
            )
            self._command_format([])
            workspace.command_input.focus()
        elif scene == "tabs-overflow":
            for index in range(2, 10):
                tab_workspace = self._add_workspace()
                tab_workspace.session.database = SimpleNamespace(
                    connection_key=("warehouse" if index % 2 else "gp"),
                    backend="gp",
                )
                tab_workspace.saved_text = ""
            self._refresh_tab(self.active_workspace)
        elif scene == "find-replace":
            self.action_open_find()
            self.query_one("#find-pattern", Input).value = "order"
            self.query_one("#replace-pattern", Input).value = "purchase"

    def _configure_results_scene(self, scene: str) -> None:
        workspace = self.active_workspace
        if scene == "query-running":
            workspace.query_state = "running"
            workspace.busy = True
            workspace.session.active_query = ExplorerQueryState(
                "sql_explorer run=visual",
                ExecutionRoute.READ,
                monotonic() - 307.5,
                None,
                "running",
            )
            self._update_status(workspace)
        elif scene == "result-success":
            workspace.editor.text = (
                "SELECT order_id, customer_name, revenue, long_note\n"
                "FROM analytics.daily_orders;\n"
                + "\n".join(f"-- Review note {index}: {'x' * 80}" for index in range(1, 43))
            )
            frame = pd.DataFrame(
                {
                    "order_id": range(1, 21),
                    "customer_name": [f"Customer {index:02d}" for index in range(1, 21)],
                    "revenue": [index * 12.75 for index in range(1, 21)],
                    "long_note": ["priority order / reviewed" * 4 for _ in range(20)],
                }
            )
            workspace.session.last_query = ExplorerQueryState(
                "sql_explorer run=visual",
                ExecutionRoute.READ,
                10.0,
                10.128,
                "completed",
            )
            workspace.last_run_result = ExplorerRunResult(
                route=ExecutionRoute.READ,
                dataframe=frame,
                displayed_rows=20,
                total_rows=1_248,
                truncated=True,
                status="Showing the first 20 of 1,248 rows.",
            )
            self.show_dataframe(frame)
            self._update_status(workspace)
        elif scene == "result-message":
            workspace.session.last_query = ExplorerQueryState(
                "sql_explorer run=visual",
                ExecutionRoute.READ,
                10.0,
                12.4,
                "failed",
            )
            self._set_notice("SQL explorer operation failed.", workspace)
            self.show_message("PermissionError: relation daily_orders is not readable", workspace)
            self._update_status(workspace)

    async def _configure_modal_scene(self, scene: str) -> None:
        workspace = self.active_workspace
        if scene in {"settings", "settings-invalid"}:
            await self._open_settings_scene(scene)
        elif scene == "file-open":
            self.push_screen(FileNavigationScreen(Path.cwd()))
        elif scene == "directory-armed":
            picker = FileNavigationScreen(Path.cwd(), select_directory=True)
            self._pending_scene_screen = picker
            await self.push_screen(picker)
            # Defer configuration past mounting, refresh and queued input/tree messages.
            picker.call_after_refresh(picker.call_later, self._settle_directory_scene, picker)
        elif scene == "new-file":
            self.push_screen(NewSqlFileScreen())
        elif scene == "mutation-confirm":
            workspace.editor.text = (
                "DELETE FROM analytics.daily_orders WHERE order_date < CURRENT_DATE - 90"
            )
            self.push_screen(
                ConfirmMutationScreen(
                    build_execution_plan(
                        workspace.editor.text,
                        "gp",
                    ),
                    db_key="gp",
                    backend="gp",
                )
            )
        elif scene == "discard-confirm":
            self.push_screen(DiscardChangesScreen(Path("quarterly_report.sql")))
        elif scene == "connections-switch-save":
            self._request_exit(restart=ConnectionsRestart(Path("/Users/analyst/new/.connections")))
        elif scene in {"save-changes", "save-changes-cancel"}:
            screen = SaveChangesScreen(workspace.file_label)
            self._pending_scene_screen = screen
            await self.push_screen(screen)
            screen.call_after_refresh(self._configure_pending_scene)
        elif scene == "export-confirm":
            self.push_screen(
                ConfirmExportScreen(
                    "Save all query result rows? This may run the query again.",
                    confirm_label="Save all",
                )
            )

    async def _open_settings_scene(self, scene: str) -> None:
        screen = SettingsScreen(
            self.session.settings,
            lambda _settings: None,
            Path("/Users/analyst/project/.connections"),
        )
        await self.push_screen(screen)
        if scene == "settings-invalid":
            screen.query_one("#settings-run-binding", Input).value = "ctrl+a"
            screen.query_one("#settings-save", Button).press()

    def _configure_workspace_scene(self, scene: str) -> None:
        if scene in {"keyboard-multicursor", "editor-command-caret", "column-ruler-scrolled"}:
            self._configure_editor_scene(scene)
        else:
            self._configure_panes_scene(scene)

    def _configure_editor_scene(self, scene: str) -> None:
        workspace = self.active_workspace
        if scene == "keyboard-multicursor":
            workspace.editor.cursor_location = (1, 14)
            workspace.editor.add_cursors(1, -1)
            workspace.editor.add_cursors(1, 1)
            self._command_keyboard(["on"])
        elif scene == "editor-command-caret":
            workspace.editor.cursor_location = (1, 14)
            workspace.command_input.value = "mv 2 15"
            workspace.command_input.focus()
        elif scene == "column-ruler-scrolled":
            workspace.editor.text = "select " + "x" * 300
            workspace.editor.cursor_location = (0, 251)

    def _configure_panes_scene(self, scene: str) -> None:
        workspace = self.active_workspace
        if scene in {"results-vertical", "results-resized"}:
            self.show_dataframe(
                pd.DataFrame(
                    {"order_id": [101, 102], "customer_name": ["Ada", "Lin"], "revenue": [125, 240]}
                )
            )
            self._command_results(["switch"] if scene == "results-vertical" else ["shrink", "4"])
        elif scene in {"help-shortcuts", "help-movement", "help-movement-end"}:
            self._command_help(["shortcuts" if scene == "help-shortcuts" else "movement"])
            if scene == "help-movement-end":
                self.set_timer(0.5, lambda: workspace.result_message.scroll_end(animate=False))

    def _configure_completion_scene(self, scene: str) -> None:
        """Set editor state before opening deterministic fake suggestions."""
        workspace = self.active_workspace
        editor = workspace.editor
        self._completion_pool.stop()
        workspace.completion = None
        suggestions = ("customer_id", "customer_name", "created_at")
        if scene == "completion-narrowed":
            editor.text = "SELECT customer_na FROM analytics.daily_orders"
            editor.cursor_location = (0, len("SELECT customer_na"))
            suggestions = ("customer_name",)
        elif scene == "completion-cte-names":
            editor.text = (
                "WITH recent_orders AS (SELECT order_id FROM analytics.orders),\n"
                "     recent_customers AS (SELECT customer_id FROM analytics.customers)\n"
                "SELECT *\nFROM recent_"
            )
            editor.cursor_location = (3, len("FROM recent_"))
            suggestions = ("recent_orders", "recent_customers")
        elif scene == "completion-where-alias":
            editor.text = "SELECT *\nFROM analytics.customers AS c\nWHERE c."
            editor.cursor_location = (2, len("WHERE c."))
        elif scene == "completion-empty-columns":
            editor.text = "SELECT  FROM analytics.customers"
            editor.cursor_location = (0, 7)
            workspace.completion_allow_empty_columns = True
        elif scene == "completion-columns":
            editor.text = (
                "SELECT c.c FROM analytics.customers c "
                "JOIN analytics.orders o ON c.id = o.customer_id"
            )
            editor.cursor_location = (0, 10)
        elif scene == "completion":
            editor.text = "SELECT *\nFROM analytics.ord"
            editor.cursor_location = (1, len("FROM analytics.ord"))
            suggestions = ("orders", "order_items", "order_returns")
        else:
            raise ValueError(f"unsupported SQL Explorer visual scene: {scene}")
        editor.focus()
        self.call_after_refresh(self._show_completion_scene, suggestions)

    def _show_completion_scene(self, suggestions: tuple[str, ...]) -> None:
        self._open_completion(self._completion_at_cursor(), suggestions, accept_single=False)
        self.scene_configured = True
        self.call_after_refresh(self._mark_scene_ready)

    def _switch_visual_database(self) -> None:
        if self._visual_database_switch_started:
            return
        self._visual_database_switch_started = True
        self._visual_database_restore_pending = True
        self._activate_tab("2")
        self._command_database(["warehouse_connection"])

    def _restore_active_focus(self) -> None:
        # Delegate the real restoration, including its queued Widget.focus application.
        super()._restore_active_focus()
        if self._visual_workspace_restore_pending:
            self._visual_workspace_restore_pending = False
            self.call_later(self._switch_visual_database)
        elif self._visual_database_restore_pending:
            self._visual_database_restore_pending = False
            self.call_later(self._focus_visual_database_command)

    def _focus_visual_database_command(self) -> None:
        # This app callback follows the restoration's app.call_later(set_focus).
        if self.ruler_trace is not None:
            self.ruler_trace.record("initialization", "restored_focus_applied")
        self.active_workspace.command_input.focus()
        if self.ruler_trace is not None:
            self.ruler_trace.record("initialization", "command_focus_requested")
        self.call_later(self._finish_visual_database_scene)

    def _finish_visual_database_scene(self) -> None:
        # Widget.focus queued its application before this one-shot continuation.
        self.scene_configured = True
        if self.ruler_trace is not None:
            self.ruler_trace.record("initialization", "command_focus_applied")
        self._poll_scene_readiness()

    def _configure_pending_scene(self) -> None:
        if self.scene_configured or self._pending_scene_screen is None:
            return
        screen = self._pending_scene_screen
        if isinstance(screen, SaveChangesScreen):
            self._focus_save_choice(screen)
        elif isinstance(screen, FileNavigationScreen):
            if self._directory_scene_initialized:
                self._configure_directory_scene(screen)
        else:
            self._configure_creation_scene(screen, self.visual_scene_id)
        if self.scene_configured:
            self._pending_scene_screen = None

    def _settle_directory_scene(self, screen: FileNavigationScreen) -> None:
        if self._pending_scene_screen is screen:
            self._directory_scene_initialized = True
            self._configure_pending_scene()

    def _configure_directory_scene(self, screen: FileNavigationScreen) -> None:
        try:
            if not self.screen_stack or self.screen is not screen or not screen.is_mounted:
                return
            details = self._scene_readiness_details()
            if not details["conditions"].get("required_controls", False):
                return
            # Use the existing Escape action once. Readiness only observes afterward.
            screen.action_cancel()
            self.scene_configured = True
            screen.call_after_refresh(self._poll_scene_readiness)
        except (NoMatches, ScreenStackError):
            return

    def _refresh_visual_evidence(self) -> None:
        self._configure_pending_scene()
        self._poll_scene_readiness()
        if not self.creation_scene_ready:
            return
        if not self._restore_primary_overflow_tab():
            return
        _refresh_evidence_if_mounted(
            self, self.visual_scene_id, self.visual_evidence_path, self.visual_manifest_path
        )

    def scene_readiness_details(self) -> dict[str, Any]:
        """Expose current scene conditions to capture and evidence consumers."""
        return self._scene_readiness_details()

    def _scene_readiness_details(self) -> dict[str, Any]:
        """Describe current conditions without treating lifecycle loss as success."""
        conditions = {"screen_available": False, "screen_mounted": False}
        details: dict[str, Any] = {
            "configured": self.scene_configured,
            "conditions": conditions,
            "missing_controls": [],
            "unmounted_controls": [],
            "incomplete_rows": [],
            "notice_text": "",
            "screen_class": None,
            "focus_id": None,
        }
        try:
            if self.screen_stack:
                screen = self.screen
                details["screen_class"] = type(screen).__name__
                conditions["screen_available"] = True
                conditions["screen_mounted"] = bool(screen.is_mounted)
                if screen.is_mounted:
                    details["focus_id"] = getattr(self.focused, "id", None)
                    _scene_control_readiness(self, screen, details)
        except (NoMatches, ScreenStackError):
            conditions["lifecycle_stable"] = False
        return details

    def _selector_readiness_snapshot(self) -> dict[str, Any]:
        """Collect failure evidence independently of production readiness."""
        return selector_readiness_snapshot(self, self.visual_scene_id)

    def _scene_state_ready(self) -> bool:
        """Do not publish a capture until its intended application state has settled."""
        if not all(self._scene_readiness_details()["conditions"].values()):
            return False
        if self.visual_scene_id in SELECTOR_SCENES:
            return all(selector_readiness_assertions(self._selector_readiness_snapshot()).values())
        return True

    def _poll_scene_readiness(self) -> None:
        self.creation_scene_ready = self.scene_configured and self._scene_state_ready()

    def _mark_scene_ready(self) -> None:
        if self._scene_state_ready():
            self.scene_configured = True
        self._poll_scene_readiness()

    def _focus_save_choice(self, screen: SaveChangesScreen) -> None:
        try:
            if not self.screen_stack or self.screen is not screen or not screen.is_mounted:
                return
            choice = "cancel" if self.visual_scene_id == "save-changes-cancel" else "save"
            button = screen.query_one(f"#save-changes-{choice}", Button)
            if not button.is_mounted:
                return
            button.focus()
            self.scene_configured = True
            screen.call_after_refresh(self._poll_scene_readiness)
        except (NoMatches, ScreenStackError):
            return

    def _restore_primary_overflow_tab(self) -> bool:
        """Restore the review tab while tolerating a screen being torn down."""
        try:
            if self.visual_scene_id != "tabs-overflow" or self.active_workspace.tab_id == "1":
                return True
            self._activate_tab("1")
        except (NoMatches, ScreenStackError):
            return False
        return False

    async def _open_creation_scene(self, scene: str) -> None:
        draft = {
            "table_name": "sandbox.customer_summary",
            "rows": [("customer_id", "BIGINT"), ("revenue", "NUMERIC(18,2)")],
            "source": "from_sql" if scene == "create-table-sql" else "table_schema",
            "from_sql": (
                "select customer_id, sum(revenue) as revenue\n"
                "from analytics.orders\ngroup by customer_id"
            ),
        }
        if scene == "create-table-invalid":
            draft["rows"] = [("customer_id", ""), ("revenue", "NUMERIC(18,2)")]
        screen = CreateTableScreen("gp", "gp", draft)
        self._pending_scene_screen = screen
        await self.push_screen(screen)
        screen.call_after_refresh(self._configure_pending_scene)

    def _configure_creation_scene(self, screen: CreateTableScreen, scene: str) -> None:
        try:
            if not self.screen_stack or self.screen is not screen or not screen.is_mounted:
                return
            if self.scene_configured:
                self._poll_scene_readiness()
                return
            # Check mounting before values(), independently of validation feedback.
            details = self._scene_readiness_details()
            if not details["conditions"].get("required_controls", False):
                return
            if scene == "create-table-types":
                field = screen.query(".create-column-type").first(Input)
                field.focus()
                field.value = "B"
            elif scene == "create-table-advanced":
                advanced = screen.query_one("#create-table-advanced", Collapsible)
                advanced.collapsed = False
                screen.query_one("#create-table-name", Input).focus()
                advanced.scroll_visible(top=True, animate=False)
            elif scene == "create-table-invalid":
                screen.query_one("#create-table-submit", Button).press()
            self.scene_configured = True
            screen.call_after_refresh(self._poll_scene_readiness)
        except (NoMatches, ScreenStackError):
            return

    def _open_visual_completion(self) -> None:
        self._open_completion(
            self._completion_at_cursor(),
            ("customer_id", "customer_name", "customer_segment", "customer_status"),
            accept_single=False,
        )


class VisualDatabasePickerApp(DatabasePickerApp):
    def __init__(self, evidence_path: Path, manifest_path: Path) -> None:
        self.visual_evidence_path = evidence_path
        self.visual_manifest_path = manifest_path
        super().__init__((("gp", "Greenplum"), ("lake", "Trino"), ("events", "ClickHouse")))

    def on_mount(self) -> None:
        super().on_mount()
        # Freeze hover for the deterministic capture.
        self.query_one(OptionList)._mouse_hovering_over = 1  # noqa: SLF001
        self.call_after_refresh(self._refresh_visual_evidence)
        self.set_interval(0.2, self._refresh_visual_evidence)

    def _refresh_visual_evidence(self) -> None:
        _refresh_evidence_if_mounted(
            self,
            "database-picker",
            self.visual_evidence_path,
            self.visual_manifest_path,
        )


class VisualConnectionsPickerScreen(ConnectionsPickerScreen):
    def __init__(self, scene_id: str) -> None:
        super().__init__()
        self.scene_id = scene_id

    def _scan(self) -> None:
        paths = (
            Path("/Users/analyst/projects/finance/.connections"),
            Path("/Users/analyst/projects/warehouse/.connections"),
            Path("/Volumes/Projects/customer-analytics/.connections"),
        )
        self._show_progress(
            DiscoveryProgress(
                () if self.scene_id == "connections-empty" else paths,
                14500,
                complete=self.scene_id != "connections-searching",
            )
        )
        if self.scene_id == "connections-error":
            self._show_error(
                "No valid SQL connections in this file. Choose another file or enter a path."
            )
        if self.scene_id in {"connections-empty", "connections-error"}:
            self.action_path()


class VisualConnectionsPickerApp(ConnectionsPickerApp):
    def __init__(self, scene_id: str, evidence_path: Path, manifest_path: Path) -> None:
        super().__init__()
        self.picker = VisualConnectionsPickerScreen(scene_id)
        self.scene_id = scene_id
        self.evidence_path = evidence_path
        self.manifest_path = manifest_path

    def on_mount(self) -> None:
        # Textual dispatches Mount to base handlers too; pushing twice places
        # the same modal above itself and recursively renders its background.
        self.set_interval(0.2, self._refresh_visual_evidence)

    def _refresh_visual_evidence(self) -> None:
        _refresh_evidence_if_mounted(self, self.scene_id, self.evidence_path, self.manifest_path)


def _scene_control_readiness(app: Any, screen: Any, details: dict[str, Any]) -> None:
    scene = app.visual_scene_id
    if scene in {"save-changes", "save-changes-cancel"}:
        _save_readiness(app, screen, details)
    elif scene == "directory-armed":
        _directory_readiness(app, screen, details)
    elif scene == "tab-database-change":
        workspace = app.active_workspace
        details["conditions"].update(
            initialization_complete=app.scene_configured,
            active_workspace=workspace.tab_id == "2",
            database=workspace.session.database.connection_key == "warehouse_connection",
            requested_focus=app.focused is workspace.command_input,
        )
    elif scene.startswith("create-table"):
        _creation_readiness(screen, scene, details)
    elif scene.startswith("completion"):
        _completion_readiness(app, details)


DIRECTORY_ARMED_NOTICE = "Enter: select this directory   Escape: cancel   Arrows: keep browsing"


def _directory_readiness(app: Any, screen: Any, details: dict[str, Any]) -> None:
    conditions = details["conditions"]
    conditions["expected_modal"] = isinstance(screen, FileNavigationScreen)
    if not conditions["expected_modal"]:
        return
    conditions["directory_mode"] = screen.select_directory
    conditions["required_controls"] = False
    for selector in (
        "#navigation-path",
        "#navigation-tree",
        "#navigation-select-directory",
        "#navigation-notice",
    ):
        _required_control(screen, selector, details)
    if details["missing_controls"] or details["unmounted_controls"]:
        return
    conditions["required_controls"] = True
    button = screen.query_one("#navigation-select-directory", Button)
    content = screen.query_one("#navigation-notice", Static).renderable
    text = content.plain if isinstance(content, Text) else str(content)
    details["notice_text"] = text[:512]
    conditions["directory_armed"] = bool(screen._directory_confirmation_armed)  # noqa: SLF001
    conditions["armed_button"] = button.has_class("armed")
    conditions["requested_focus"] = app.focused is button
    conditions["armed_notice"] = text == DIRECTORY_ARMED_NOTICE


def _save_readiness(app: Any, screen: Any, details: dict[str, Any]) -> None:
    conditions = details["conditions"]
    conditions["expected_modal"] = isinstance(screen, SaveChangesScreen)
    if not conditions["expected_modal"]:
        return
    choice = "cancel" if app.visual_scene_id == "save-changes-cancel" else "save"
    conditions["document_identity"] = screen.title == app.active_workspace.file_label
    selector = f"#save-changes-{choice}"
    conditions["required_controls"] = False
    try:
        button = screen.query_one(selector, Button)
    except NoMatches:
        details["missing_controls"].append(selector)
        return
    conditions["required_controls"] = bool(button.is_mounted)
    if not button.is_mounted:
        details["unmounted_controls"].append(selector)
    conditions["requested_focus"] = app.focused is button


def _creation_readiness(screen: Any, scene: str, details: dict[str, Any]) -> None:
    conditions = details["conditions"]
    conditions["expected_modal"] = isinstance(screen, CreateTableScreen)
    if not conditions["expected_modal"]:
        return
    conditions["required_controls"] = False
    selectors = (
        "#create-table-name",
        "#create-table-source",
        "#create-from-sql",
        "#create-insert_data",
        "#create-skip_if_exists",
        "#create-drop_if_exists",
        "#create-table-submit",
        "#create-table-notice",
    )
    for selector in selectors:
        _required_control(screen, selector, details)
    if details["missing_controls"] or details["unmounted_controls"]:
        return
    values = screen.values()
    conditions["required_controls"] = all(
        field.is_mounted for field in screen.query(".create-column-name, .create-column-type")
    )
    if scene == "create-table-invalid":
        details["incomplete_rows"] = [
            index for index, (name, kind) in enumerate(values["rows"]) if not name or not kind
        ][:32]
        content = screen.query_one("#create-table-notice", Static).renderable
        if isinstance(content, Text):
            text = content.plain
        elif isinstance(content, str):
            text = content
        else:
            message = "Validation notice must contain plain text"
            raise TypeError(message)
        details["notice_text"] = text[:512]
        conditions["incomplete_rows"] = bool(details["incomplete_rows"])
        conditions["validation_feedback"] = "Each column needs" in text


def _required_control(screen: Any, selector: str, details: dict[str, Any]) -> None:
    try:
        control = screen.query_one(selector)
    except NoMatches:
        details["missing_controls"].append(selector)
    else:
        if not control.is_mounted:
            details["unmounted_controls"].append(selector)


def _completion_readiness(app: Any, details: dict[str, Any]) -> None:
    workspace = app.active_workspace
    editor, menu = workspace.editor, workspace.completion_menu
    conditions = details["conditions"]
    conditions["popup_open"] = menu.is_open
    conditions["popup_mounted"] = menu.is_mounted
    conditions["popup_visible"] = menu.display and menu.visible
    viewport = editor.scrollable_content_region
    screen = app.screen.content_region
    popup = menu.region
    _, cursor_y = editor.wrapped_document.location_to_offset(editor.cursor_location)
    row = viewport.y + cursor_y - int(editor.scroll_y)
    conditions["cursor_visible"] = viewport.y <= row < viewport.bottom
    conditions["popup_positioned"] = (
        popup.width > 0
        and popup.height >= 3
        and screen.x <= popup.x < popup.right <= screen.right
        and viewport.y <= popup.y < popup.bottom <= viewport.bottom
        and (popup.y > row or popup.bottom <= row)
    )
    composed = app.screen._compositor.render_strips()  # noqa: SLF001
    text = "".join(
        strip.crop(popup.x, popup.right).text
        for strip in composed[max(0, popup.y) : max(0, popup.bottom)]
    )
    conditions["popup_composed"] = bool(menu.suggestions) and all(
        suggestion in text for suggestion in menu.suggestions[: min(3, len(menu.suggestions))]
    )


def _manifest_scene(manifest_path: Path, scene_id: str) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return next(scene for scene in manifest["scenes"] if scene["id"] == scene_id)


def _widget_geometry(widget: Widget) -> dict[str, Any]:
    region = widget.region
    return {
        "x": region.x,
        "y": region.y,
        "width": region.width,
        "height": region.height,
        "right": region.right,
        "bottom": region.bottom,
        "display": widget.display,
    }


def _refresh_evidence_if_mounted(
    app: App[Any],
    scene_id: str,
    evidence_path: Path,
    manifest_path: Path,
) -> bool:
    """Refresh evidence while a scene is mounted, tolerating normal teardown."""
    try:
        if not app.screen_stack:
            return False
        screen = app.screen
        if not screen.is_mounted:
            return False
        _write_evidence(app, scene_id, evidence_path, manifest_path)
    except (NoMatches, ScreenStackError):
        return False
    return True


def _pane_gaps(workspace: Any, panes: list[Any]) -> bool:
    if workspace.results_open and workspace.results_orientation == "vertical":
        editor_pane, result_pane, command_pane = panes
        return (
            result_pane.region.x - editor_pane.region.right == 1
            and command_pane.region.y - max(editor_pane.region.bottom, result_pane.region.bottom)
            == 1
        )
    return all(right.region.y - left.region.bottom == 1 for left, right in zip(panes, panes[1:]))


def _required_element_evidence(
    app: App[Any], required: list[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, bool]]:
    elements: dict[str, dict[str, Any]] = {}
    assertions: dict[str, bool] = {}
    for selector in required:
        matches = [
            candidate
            for candidate in app.screen.query(selector)
            if candidate.region.width > 0 and candidate.region.height > 0
        ]
        if not matches:
            assertions[f"present:{selector}"] = False
            continue
        widget = matches[0]
        geometry = _widget_geometry(widget)
        elements[selector] = geometry
        assertions[f"present:{selector}"] = bool(
            geometry["display"] and geometry["width"] > 0 and geometry["height"] > 0
        )

    return elements, assertions


def _workspace_assertions(app: VisualExplorerApp, scene_id: str) -> dict[str, bool]:
    assertions: dict[str, bool] = {}
    workspace = app.active_workspace
    editor = workspace.editor
    editor_status = workspace.query_one("#editor-status")
    assertions["editor_status_no_overlap"] = editor.region.bottom == editor_status.region.y
    assertions["editor_first_two_lines_visible"] = (
        editor.content_region.height >= 2 and editor.scroll_offset.y == 0
    )
    tab = app.query_one("#tab-1")
    assertions["compact_tab_height"] = tab.region.height == 1
    if scene_id == "tabs-overflow":
        assertions["tabs_overflow_first_tab_active"] = workspace.tab_id == "1"
        assertions["overflow_keeps_tab_labels_visible"] = (
            app.query_one("#tab-strip").scrollable_content_region.height >= 1
        )
    summary = workspace.query_one("#query-summary")
    interrupt = workspace.query_one("#interrupt", Button)
    run = workspace.query_one("#run-query", Button)
    assertions["run_spelling"] = str(run.label) == "RUN"
    assertions["run_visible_only_ready"] = run.display == (
        not workspace.busy and workspace.query_state == "ready"
    )
    assertions["run_right_aligned"] = (
        not run.display or run.region.right == summary.content_region.right
    )
    assertions["stop_spelling"] = str(interrupt.label) == "STOP"
    assertions["interrupt_right_aligned"] = (
        not interrupt.display or interrupt.region.right == summary.content_region.right
    )
    assertions["interrupt_visible_only_running"] = interrupt.display == (
        workspace.query_state in {"running", "cancelling"}
    )
    _assert_changed_controls(app, scene_id, assertions)

    panes = [workspace.query_one(".query-pane")]
    if workspace.results_open:
        panes.append(workspace.query_one(".result-pane"))
    panes.append(workspace.query_one(".command-panel"))
    assertions["pane_gaps"] = _pane_gaps(workspace, panes)
    table = workspace.query_one(ResultTable)
    for name, widget in (("editor", editor), ("result", table)):
        if widget.show_vertical_scrollbar:
            assertions[f"{name}_scrollbar_right"] = (
                widget.vertical_scrollbar.region.right == widget.content_region.right
            )

    return assertions


def _ruler_evidence(app: VisualExplorerApp) -> dict[str, Any]:
    """Observe two separate terminal rows without moving focus, scroll or cursors."""
    workspace = app.active_workspace
    editor = workspace.editor
    ruler = workspace.query_one("#column-ruler", Static)
    composed = app.screen._compositor.render_strips()  # noqa: SLF001
    rows = {}
    for name, widget in (("ruler", ruler), ("editor", editor)):
        region = widget.content_region
        original = widget.render_line(0)
        composed_original = composed[region.y]
        composed_crop = composed_original.crop(region.x, region.right)
        widget_row = strip_record(original, x=region.x, y=region.y, width=region.width)
        compositor_row = strip_record(composed_crop, x=region.x, y=region.y, width=region.width)
        styled = styled_content_row(widget)
        rows[name] = {
            "styled_widget_row": styled["row"],
            "styled_widget_observation": styled,
            "region": _widget_geometry(widget),
            "widget_row": widget_row,
            "compositor_row": compositor_row,
            "strip_observation": {
                "widget": ruler_strip_observation(original, widget_row, width=region.width),
                "compositor": ruler_strip_observation(
                    composed_crop, compositor_row, width=region.width
                ),
                "compositor_screen_lengths": {
                    "characters": len(composed_original.text),
                    "cells": composed_original.cell_length,
                },
                "compositor_crop": [region.x, region.right],
            },
        }
    return {
        "active_workspace": workspace.tab_id,
        "cursor_location": list(editor.cursor_location),
        "active_column": editor.cursor_location[1] + 1,
        "cursor_render_offset": list(editor.cursor_render_offset),
        "scroll_x": int(editor.scroll_x),
        "scroll_y": int(editor.scroll_y),
        "gutter_width": editor.gutter_width,
        "gutter_left": editor.gutter.left,
        "active_column_screen_x": (
            editor.content_region.x
            + editor.gutter_width
            + editor.cursor_render_offset[0]
            - int(editor.scroll_x)
        ),
        "document_empty": editor.text == "",
        "lifecycle": ruler_lifecycle_snapshot(app, rows),
        "callback_trace": (
            app.ruler_trace.snapshot()
            if app.ruler_trace is not None
            else {"complete": False, "unavailable": "trace_not_installed"}
        ),
        **rows,
    }


def _ruler_evidence_assertions(evidence: dict[str, Any]) -> dict[str, bool]:
    ruler, editor = evidence["ruler"], evidence["editor"]
    ruler_region, editor_region = ruler["region"], editor["region"]
    label = str(evidence["active_column"])
    screen_x = evidence["active_column_screen_x"]
    label_visible = all(
        row is not None
        and 0 <= screen_x - row["x"] < row["cells"]
        and row["text"][screen_x - row["x"] - len(label) + 1 : screen_x - row["x"] + 1] == label
        for row in (ruler["widget_row"], ruler["styled_widget_row"], ruler["compositor_row"])
    )
    return {
        "ruler:styled_layers_equal": all(
            entry["styled_widget_observation"]["complete"]
            and rendered_rows_equal(entry["styled_widget_row"], entry["compositor_row"])
            for entry in (ruler, editor)
        ),
        "ruler:separate_rows": (
            ruler_region["height"] == 1
            and ruler_region["bottom"] == editor_region["y"]
            and ruler_region["x"] == editor_region["x"]
        ),
        "ruler:active_column_cell": (
            screen_x
            == ruler_region["x"]
            + evidence["gutter_left"]
            + evidence["gutter_width"]
            + evidence["cursor_render_offset"][0]
            - evidence["scroll_x"]
            and label_visible
        ),
        "ruler:first_gutter_separate": all(
            row is not None
            and row["text"][: evidence["gutter_width"]].strip() == "1"
            and row["y"] > ruler["compositor_row"]["y"]
            for row in (editor["widget_row"], editor["styled_widget_row"], editor["compositor_row"])
        ),
        "ruler:empty_workspace": (
            evidence["active_workspace"] == "2"
            and evidence["document_empty"]
            and evidence["cursor_location"] == [0, 0]
        ),
    }


def _persist_evidence(payload: dict[str, Any], evidence_path: Path) -> None:
    # A mounted scene may emit an incomplete diagnostic frame before it settles,
    # but teardown must never replace geometry that already passed every check.
    if not payload["ok"] and evidence_path.exists():
        return
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = evidence_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(evidence_path)


def _new_file_alignment_assertions(app: App[Any]) -> dict[str, bool]:
    screen = app.screen
    viewport = screen.content_region
    dialog = screen.query_one("#new-file-dialog").region
    left, right = dialog.x - viewport.x, viewport.right - dialog.right
    top, bottom = dialog.y - viewport.y, viewport.bottom - dialog.bottom
    return {
        "new_file_center_middle": (
            screen.styles.align_horizontal == "center" and screen.styles.align_vertical == "middle"
        ),
        "new_file_dialog_contained": (
            dialog.width > 0 and dialog.height > 0 and min(left, right, top, bottom) >= 0
        ),
        "new_file_horizontally_centered": abs(left - right) <= 1,
        "new_file_vertically_centered": abs(top - bottom) <= 1,
    }


def _optional_scene_evidence(
    app: App[Any], scene_id: str, assertions: dict[str, bool]
) -> dict[str, Any]:
    """Collect optional observations in their original order before publication."""
    selector_snapshot = None
    if scene_id in SELECTOR_SCENES:
        selector_snapshot = selector_readiness_snapshot(app, scene_id)
        assertions.update(selector_readiness_assertions(selector_snapshot))
    readiness = None
    if scene_id == "directory-armed" and isinstance(app, VisualExplorerApp):
        readiness = app.scene_readiness_details()
        assertions.update(
            {f"directory:{name}": value for name, value in readiness["conditions"].items()}
        )
    assertions.update(_button_color_assertions(app))
    if scene_id == "new-file":
        assertions.update(_new_file_alignment_assertions(app))
    ruler_evidence = None
    if scene_id == "tab-database-change" and isinstance(app, VisualExplorerApp):
        ruler_evidence = _ruler_evidence(app)
        assertions.update(_ruler_evidence_assertions(ruler_evidence))

    return {
        key: value
        for key, value in (
            ("scene_readiness", readiness),
            ("selector_readiness", selector_snapshot),
            ("ruler_evidence", ruler_evidence),
        )
        if value is not None
    }


def _write_evidence(
    app: App[Any],
    scene_id: str,
    evidence_path: Path,
    manifest_path: Path,
) -> None:
    # A final capture owns its diagnostics. Timers and teardown may refresh geometry,
    # but must not replace the exported frame's evidence.
    if evidence_path.exists():
        previous = json.loads(evidence_path.read_text(encoding="utf-8"))
        if "render_diagnostics" in previous:
            return
    required = _manifest_scene(manifest_path, scene_id)["required_elements"]
    elements, assertions = _required_element_evidence(app, required)
    if os.environ.get("SQL_EXPLORER_VISUAL_REQUIRE_COLOR_256") == "1":
        assertions["color_256_renderer"] = app.console.color_system == "256"
    if isinstance(app, VisualExplorerApp) and len(app.screen_stack) == 1:
        assertions.update(_workspace_assertions(app, scene_id))

    optional = _optional_scene_evidence(app, scene_id, assertions)

    payload = {
        "schema_version": 1,
        "scene_id": scene_id,
        "terminal": os.environ.get("TERM", ""),
        "color_system": app.console.color_system,
        "screen": {"width": app.size.width, "height": app.size.height},
        "elements": elements,
        "assertions": assertions,
        "ok": bool(assertions) and all(assertions.values()),
    }
    payload.update(optional)
    _persist_evidence(payload, evidence_path)


def _button_color_assertions(app: App[Any]) -> dict[str, bool]:
    assertions = {}
    for index, button in enumerate(app.screen.query("ModalScreen Button, FindReplaceBar Button")):
        focused = button.has_focus and not button.disabled
        color = app.get_css_variables()["accent" if focused else "surface"]
        identity = button.id
        if identity is None:
            classes = ".".join(sorted(button.classes))
            identity = f"{type(button).__name__}.{classes}[{index}]"
        assertions[f"button_focus_color:{identity}"] = button.styles.background == Color.parse(
            color
        )
    return assertions


def _assert_changed_controls(
    app: VisualExplorerApp, scene_id: str, assertions: dict[str, bool]
) -> None:
    workspace = app.active_workspace
    editor = workspace.editor
    ruler = workspace.query_one("#column-ruler")
    assertions["column_ruler_aligned"] = (
        ruler.region.bottom == editor.region.y and ruler.region.x == editor.region.x
    )
    assertions["column_ruler_single_row"] = ruler.region.height == 1
    if scene_id == "editor-command-caret":
        assertions["command_focused_with_editor_caret"] = app.focused is workspace.command_input
    if scene_id == "column-ruler-scrolled":
        assertions["ruler_scrolled"] = editor.scroll_x > 0
    if scene_id == "help-movement-end":
        message = workspace.result_message
        assertions["help_scrolled_to_end"] = message.scroll_y == message.max_scroll_y > 0
    if scene_id == "find-replace":
        assertions["find_close_above_input"] = (
            workspace.query_one("#close-find").region.bottom
            <= workspace.query_one("#find-pattern").region.y
        )
    if scene_id == "tab-database-change":
        button = app.query_one("#tab-2 .tab-select", TabSelectButton)
        assertions["updated_tab_title_visible"] = (
            "[warehouse_connection] Untitled 2" in button.render_line(0).text
        )
    if scene_id == "formatted-sql":
        assertions["formatted_script_visible"] = (
            "select\n    order_id," in editor.text and ";\n\nselect" in editor.text
        )
    if scene_id == "query-running":
        indicator = workspace.query_one("#query-running-indicator")
        outcome = workspace.query_one("#query-outcome")
        assertions["snake_loop_matches_query_card_height"] = (
            indicator.region.y == outcome.region.y
            and indicator.region.bottom == outcome.region.bottom
            and all(indicator.render_line(row).text.strip() for row in range(3))
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    return parser


def create_scene(scene: str, evidence: Path, manifest: Path) -> App[Any]:
    if scene == "database-picker":
        app: App[Any] = VisualDatabasePickerApp(evidence, manifest)
    elif scene in {
        "connections-picker",
        "connections-searching",
        "connections-empty",
        "connections-error",
    }:
        app = VisualConnectionsPickerApp(scene, evidence, manifest)
    else:
        app = VisualExplorerApp(scene, evidence, manifest)
    return app


def main() -> int:
    args = _parser().parse_args()
    app = create_scene(args.scene, args.evidence, args.manifest)
    app.run(mouse=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
