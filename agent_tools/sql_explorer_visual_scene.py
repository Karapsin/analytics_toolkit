#!/usr/bin/env python3
"""Render deterministic SQL Explorer scenes for the macOS visual-review guest."""

# ruff: noqa: EM102, FLY002, PLR0915, PLR2004, TRY003

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
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.create_table_screen import CreateTableScreen
from analytics_toolkit.sql_explorer.exports import ConfirmExportScreen
from analytics_toolkit.sql_explorer.file_commands import NewSqlFileScreen
from analytics_toolkit.sql_explorer.picker import DatabasePickerApp
from analytics_toolkit.sql_explorer.runtime import (
    ExplorerCancelResult,
    ExplorerQueryState,
    ExplorerRunResult,
)
from analytics_toolkit.sql_explorer.settings import ExplorerSettings
from analytics_toolkit.sql_explorer.statements import ExecutionRoute, build_execution_plan
from analytics_toolkit.sql_explorer.tabs import SaveChangesScreen, TabSelectButton
from analytics_toolkit.sql_explorer.widgets import (
    ConfirmMutationScreen,
    DiscardChangesScreen,
    FileNavigationScreen,
    ResultTable,
)
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
            scene_id.startswith("create-table")
            or scene_id in {"command-completion", "database-completion"}
        )
        super().__init__(VisualSession())

    def on_mount(self) -> None:
        super().on_mount()
        self.call_after_refresh(self._configure_visual_scene)

    def _configure_visual_scene(self) -> None:  # noqa: C901, PLR0912
        workspace = self.active_workspace
        workspace.editor.text = "\n".join(
            (
                "SELECT order_id, customer_name, revenue",
                "FROM analytics.daily_orders",
                "WHERE order_date >= CURRENT_DATE - INTERVAL '7 days';",
            )
        )
        scene = self.visual_scene_id
        if scene.startswith("create-table"):
            self._open_creation_scene(scene)
        elif scene == "completion-narrowed":
            workspace.editor.text = "SELECT customer_na FROM analytics.daily_orders"
            workspace.editor.cursor_location = (0, len("SELECT customer_na"))
            self.set_timer(
                0.3,
                lambda: self._open_completion(
                    self._completion_at_cursor(), ("customer_name",), accept_single=False
                ),
            )
        elif scene in {"command-completion", "database-completion"}:
            command = workspace.command_input
            command.value = "db wa" if scene == "database-completion" else "c"
            command.cursor_position = len(command.value)
            command.database_keys = ("warehouse", "warehouse_dev")
            command.focus()

            def show_commands() -> None:
                command.refresh_completion()
                self.call_after_refresh(self._mark_scene_ready)

            self.set_timer(0.3, show_commands)
        elif scene in {"completion-columns", "completion-empty-columns"}:
            workspace.editor.text = (
                "SELECT c.c FROM analytics.customers c "
                "JOIN analytics.orders o ON c.id = o.customer_id"
            )
            if scene == "completion-empty-columns":
                workspace.editor.text = "SELECT  FROM analytics.customers"
                workspace.editor.cursor_location = (0, 7)
                workspace.completion_allow_empty_columns = True
            else:
                workspace.editor.cursor_location = (0, 10)
            self.set_timer(
                0.3,
                lambda: self._open_completion(
                    self._completion_at_cursor(),
                    ("customer_id", "customer_name", "created_at"),
                    accept_single=False,
                ),
            )
        elif scene == "tab-database-change":
            self._add_workspace()
            self.set_timer(0.3, self._switch_visual_database)
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
        elif scene == "completion":
            self._completion_pool.stop()
            workspace.completion = None
            workspace.editor.text = "SELECT customer_"
            workspace.editor.cursor_location = (0, len(workspace.editor.text))
            self.set_timer(0.3, self._open_visual_completion)
        elif scene == "find-replace":
            self.action_open_find()
            self.query_one("#find-pattern", Input).value = "order"
            self.query_one("#replace-pattern", Input).value = "purchase"
        elif scene == "query-running":
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
            workspace.editor.text = "\n".join(
                f"SELECT {index} AS row_number, '{'x' * 80}' AS wide_value"
                for index in range(1, 45)
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
                dataframe=pd.DataFrame(),
                displayed_rows=20,
                total_rows=1_248,
                truncated=True,
                status="Showing the first 20 of 1,248 rows.",
            )
            self.show_dataframe(
                pd.DataFrame(
                    {
                        "order_id": range(1, 61),
                        "customer_name": [f"Customer {index:02d}" for index in range(1, 61)],
                        "revenue": [index * 12.75 for index in range(1, 61)],
                        "long_note": ["priority order / reviewed" * 4 for _ in range(60)],
                    }
                )
            )
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
        elif scene == "file-open":
            self.push_screen(FileNavigationScreen(Path.cwd()))
        elif scene == "directory-armed":
            picker = FileNavigationScreen(Path.cwd(), select_directory=True)
            self.push_screen(picker)
            self.call_after_refresh(picker.action_cancel)
        elif scene == "new-file":
            self.push_screen(NewSqlFileScreen())
        elif scene == "mutation-confirm":
            self.push_screen(
                ConfirmMutationScreen(
                    build_execution_plan(
                        "DELETE FROM analytics.daily_orders WHERE order_date < CURRENT_DATE - 90",
                        "gp",
                    ),
                    db_key="gp",
                    backend="gp",
                )
            )
        elif scene == "discard-confirm":
            self.push_screen(DiscardChangesScreen(Path("quarterly_report.sql")))
        elif scene in {"save-changes", "save-changes-cancel"}:
            self.push_screen(SaveChangesScreen("quarterly_report.sql"))
            if scene == "save-changes-cancel":
                self.call_after_refresh(
                    lambda: self.screen.query_one("#save-changes-cancel", Button).focus()
                )
        elif scene == "export-confirm":
            self.push_screen(
                ConfirmExportScreen(
                    "Save all query result rows? This may run the query again.",
                    confirm_label="Save all",
                )
            )
        elif scene != "editor-ready":
            raise ValueError(f"unsupported SQL Explorer visual scene: {scene}")

        self.call_after_refresh(self._refresh_visual_evidence)
        self.set_interval(0.2, self._refresh_visual_evidence)

    def _switch_visual_database(self) -> None:
        self._activate_tab("2")
        self._command_database(["warehouse_connection"])
        self.active_workspace.command_input.focus()

    def _refresh_visual_evidence(self) -> None:
        if not self.creation_scene_ready:
            return
        if not self._restore_primary_overflow_tab():
            return
        _refresh_evidence_if_mounted(
            self, self.visual_scene_id, self.visual_evidence_path, self.visual_manifest_path
        )

    def _mark_scene_ready(self) -> None:
        self.creation_scene_ready = True

    def _restore_primary_overflow_tab(self) -> bool:
        """Restore the review tab while tolerating a screen being torn down."""
        try:
            if self.visual_scene_id != "tabs-overflow" or self.active_workspace.tab_id == "1":
                return True
            self._activate_tab("1")
        except (NoMatches, ScreenStackError):
            return False
        return False

    def _open_creation_scene(self, scene: str) -> None:
        draft = {
            "table_name": "sandbox.customer_summary",
            "rows": [("customer_id", "BIGINT"), ("revenue", "NUMERIC(18,2)")],
            "source": "from_sql" if scene == "create-table-sql" else "table_schema",
            "from_sql": (
                "select customer_id, sum(revenue) as revenue\n"
                "from analytics.orders\ngroup by customer_id"
            ),
        }
        screen = CreateTableScreen("gp", "gp", draft)
        self.push_screen(screen)

        def configure() -> None:
            if not screen.is_mounted:
                return
            if scene == "create-table-types":
                field = screen.query(".create-column-type").first(Input)
                field.focus()
                field.value = "B"
            elif scene == "create-table-advanced":
                advanced = screen.query_one("#create-table-advanced", Collapsible)
                advanced.collapsed = False
                advanced.scroll_visible(top=True, animate=False)
            elif scene == "create-table-invalid":
                screen.query_one("#create-table-notice", Static).update(
                    "Each column needs a name and SQL type."
                )
            self.creation_scene_ready = True

        self.set_timer(0.3, configure)

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
        self.query_one(OptionList)._mouse_hovering_over = 1  # noqa: SLF001 - freeze hover for VNC.
        self.call_after_refresh(self._refresh_visual_evidence)
        self.set_interval(0.2, self._refresh_visual_evidence)

    def _refresh_visual_evidence(self) -> None:
        _refresh_evidence_if_mounted(
            self,
            "database-picker",
            self.visual_evidence_path,
            self.visual_manifest_path,
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
    if not app.screen_stack:
        return False
    try:
        _write_evidence(app, scene_id, evidence_path, manifest_path)
    except (NoMatches, ScreenStackError):
        return False
    return True


def _write_evidence(
    app: App[Any],
    scene_id: str,
    evidence_path: Path,
    manifest_path: Path,
) -> None:
    required = _manifest_scene(manifest_path, scene_id)["required_elements"]
    elements: dict[str, dict[str, Any]] = {}
    assertions: dict[str, bool] = {}
    if os.environ.get("SQL_EXPLORER_VISUAL_REQUIRE_COLOR_256") == "1":
        assertions["color_256_renderer"] = app.console.color_system == "256"
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

    if isinstance(app, VisualExplorerApp) and len(app.screen_stack) == 1:
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
        assertions["pane_gaps"] = all(
            right.region.y - left.region.bottom == 1 for left, right in zip(panes, panes[1:])
        )
        table = workspace.query_one(ResultTable)
        for name, widget in (("editor", editor), ("result", table)):
            if widget.show_vertical_scrollbar:
                assertions[f"{name}_scrollbar_right"] = (
                    widget.vertical_scrollbar.region.right == widget.content_region.right
                )

    assertions.update(_button_color_assertions(app))

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
    # A mounted scene may emit an incomplete diagnostic frame before it settles,
    # but teardown must never replace geometry that already passed every check.
    if not payload["ok"] and evidence_path.exists():
        return
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = evidence_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(evidence_path)


def _button_color_assertions(app: App[Any]) -> dict[str, bool]:
    assertions = {}
    for button in app.screen.query("ModalScreen Button, FindReplaceBar Button"):
        focused = button.has_focus and not button.disabled
        color = app.get_css_variables()["accent" if focused else "surface"]
        assertions[f"button_focus_color:{button.id}"] = button.styles.background == Color.parse(
            color
        )
    return assertions


def _assert_changed_controls(
    app: VisualExplorerApp, scene_id: str, assertions: dict[str, bool]
) -> None:
    workspace = app.active_workspace
    editor = workspace.editor
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


def main() -> int:
    args = _parser().parse_args()
    if args.scene == "database-picker":
        app: App[Any] = VisualDatabasePickerApp(args.evidence, args.manifest)
    else:
        app = VisualExplorerApp(args.scene, args.evidence, args.manifest)
    app.run(mouse=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
