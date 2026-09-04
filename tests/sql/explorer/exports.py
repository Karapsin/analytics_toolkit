from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pandas as pd
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.errors import SqlExplorerConfigurationError
from analytics_toolkit.sql_explorer.exports import (
    ConfirmExportScreen,
    SqlExplorerExportCommandsMixin,
)
from analytics_toolkit.sql_explorer.file_commands import NewFileScreen
from analytics_toolkit.sql_explorer.statements import build_execution_plan
from analytics_toolkit.sql_explorer.widgets import ConfirmMutationScreen, FileNavigationScreen
from textual.widgets import Input

from tests.sql.explorer.app import FakeSession

if TYPE_CHECKING:
    from pathlib import Path


class ExportSession(FakeSession):
    def __init__(self, dataframe: pd.DataFrame, *, truncated: bool = False) -> None:
        super().__init__()
        plan = self.plan("select value from sample")
        self.dataframe = dataframe
        self.state = SimpleNamespace(
            plan=plan,
            dataframe=None if truncated else dataframe,
            truncated=truncated,
        )

    def export_state(self) -> SimpleNamespace:
        return self.state

    def export_dataframe(self) -> pd.DataFrame:
        return self.dataframe.copy()


class FailingExportSession(ExportSession):
    def export_dataframe(self) -> pd.DataFrame:
        raise RuntimeError


class ExportHarness(SqlExplorerExportCommandsMixin):
    def __init__(self, state: SimpleNamespace) -> None:
        self.session = SimpleNamespace(
            export_state=lambda: state,
            settings=SimpleNamespace(confirm_mutations=True),
            database=SimpleNamespace(connection_key="gp", backend="gp"),
        )
        self.workspace = SimpleNamespace(
            tab_id="1",
            busy=False,
            cancelling=False,
            closing=False,
            results_open=True,
            query_state="ready",
            session=self.session,
            result_table=SimpleNamespace(styles=SimpleNamespace(display="block")),
            reset_query_state=lambda: None,
        )
        self._active_tab_id = "1"
        self._workspaces = {"1": self.workspace}
        self.errors: list[Exception] = []
        self.notices: list[str] = []
        self.screens: list[tuple[object, Any]] = []
        self.writes: list[tuple[str, Path]] = []
        self.status_updates = 0
        self.removed: list[str] = []
        self.exit_checks = 0

    def query_one(self, _selector: str, _widget_type: object) -> SimpleNamespace:
        return SimpleNamespace(styles=SimpleNamespace(display="block"))

    @property
    def active_workspace(self) -> SimpleNamespace:
        return self.workspace

    @property
    def busy(self) -> bool:
        return bool(self.workspace.busy)

    @busy.setter
    def busy(self, value: bool) -> None:
        self.workspace.busy = value

    @property
    def results_open(self) -> bool:
        return bool(self.workspace.results_open)

    @results_open.setter
    def results_open(self, value: bool) -> None:
        self.workspace.results_open = value

    def show_error(self, error: Exception, _workspace: object = None) -> None:
        self.errors.append(error)

    def _set_notice(self, notice: str, _workspace: object = None) -> None:
        self.notices.append(notice)

    def _update_status(self, _workspace: object = None) -> None:
        self.status_updates += 1

    def push_screen(self, screen: object, callback: Any) -> None:
        self.screens.append((screen, callback))

    def _export_in_worker(self, output_format: str, path: Path, *_args: object) -> None:
        self.writes.append((output_format, path))
        self.workspace.busy = False
        self.workspace.query_state = "ready"
        self.workspace.operation_database = None

    def _drain_query_queue(self) -> None:
        pass

    def _database_is_busy(self, connection_key: str) -> bool:
        normalized = connection_key.casefold()
        return any(
            workspace.busy
            and (
                getattr(workspace, "operation_database", None) or workspace.session.database
            ).connection_key.casefold()
            == normalized
            for workspace in self._workspaces.values()
        )

    def _remove_workspace(self, tab_id: str) -> None:
        self.removed.append(tab_id)

    def _finish_exit_if_ready(self) -> None:
        self.exit_checks += 1


async def _wait_for(pilot: Any, predicate: Any) -> None:
    for _ in range(20):
        if predicate():
            return
        await pilot.pause()
    assert predicate()


def test_csv_export_uses_filename_and_directory_picker(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)

    async def exercise() -> None:
        dataframe = pd.DataFrame({"value": [1, 2]})
        application = SqlExplorerApp(ExportSession(dataframe))
        async with application.run_test() as pilot:
            application.show_dataframe(dataframe)
            application._command_to_csv([])
            await pilot.pause()
            screen = application.screen
            assert isinstance(screen, NewFileScreen)
            screen.query_one("#new-file-name", Input).value = "result.csv"
            screen.action_confirm()
            await pilot.pause()
            picker = application.screen
            assert isinstance(picker, FileNavigationScreen)
            picker.action_choose_directory()

            output = tmp_path / "result.csv"
            await _wait_for(pilot, output.exists)
            assert pd.read_csv(output).to_dict(orient="list") == {"value": [1, 2]}

    asyncio.run(exercise())


def test_excel_export_omits_dataframe_index(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[Path, bool]] = []

    def fake_to_excel(self: pd.DataFrame, path: Path, *, index: bool) -> None:
        calls.append((path, index))

    monkeypatch.setattr(pd.DataFrame, "to_excel", fake_to_excel)

    async def exercise() -> None:
        dataframe = pd.DataFrame({"value": [1]})
        application = SqlExplorerApp(ExportSession(dataframe))
        async with application.run_test() as pilot:
            application.show_dataframe(dataframe)
            application._write_export("excel", tmp_path / "result.xlsx")
            await _wait_for(pilot, lambda: bool(calls))
            assert calls == [(tmp_path / "result.xlsx", False)]

    asyncio.run(exercise())


def test_export_worker_surfaces_write_errors(tmp_path: Path) -> None:
    async def exercise() -> None:
        dataframe = pd.DataFrame({"value": [1]})
        application = SqlExplorerApp(FailingExportSession(dataframe))
        async with application.run_test() as pilot:
            application.show_dataframe(dataframe)
            application._write_export("csv", tmp_path / "result.csv")
            await _wait_for(pilot, lambda: not application.busy)

    asyncio.run(exercise())


def test_truncated_export_and_existing_destination_require_confirmation(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "result.csv"
    existing.write_text("original", encoding="utf-8")

    async def exercise() -> None:
        dataframe = pd.DataFrame({"value": [1]})
        application = SqlExplorerApp(ExportSession(dataframe, truncated=True))
        async with application.run_test() as pilot:
            application.show_dataframe(dataframe)
            application._command_to_csv([])
            screen = application.screen
            assert isinstance(screen, ConfirmExportScreen)
            screen.action_cancel()
            await pilot.pause()
            assert existing.read_text(encoding="utf-8") == "original"

            application._export_directory_selected("csv", "result.csv", tmp_path)
            screen = application.screen
            assert isinstance(screen, ConfirmExportScreen)
            screen.action_cancel()
            await pilot.pause()
            assert existing.read_text(encoding="utf-8") == "original"

    asyncio.run(exercise())


def test_export_confirmation_screen_actions() -> None:
    async def exercise() -> None:
        screen = ConfirmExportScreen("Save all query results?", confirm_label="Save all")
        dismiss = Mock()
        focus = Mock()
        screen.dismiss = dismiss
        screen.query_one = Mock(return_value=SimpleNamespace(focus=focus))

        screen.action_confirm()
        screen.action_cancel()
        screen.action_select_confirm()
        screen.action_select_cancel()
        screen.on_button_pressed(SimpleNamespace(button=SimpleNamespace(id="export-confirm")))
        screen.on_button_pressed(SimpleNamespace(button=SimpleNamespace(id="export-cancel")))

        assert dismiss.call_args_list[-2:] == [((True,), {}), ((False,), {})]
        assert focus.call_count == 2

    asyncio.run(exercise())


def test_export_command_error_and_destination_paths(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)

    async def exercise() -> None:
        plan = build_execution_plan("select value from sample", "gp")
        state = SimpleNamespace(plan=plan, dataframe=pd.DataFrame({"value": [1]}), truncated=False)
        application = ExportHarness(state)

        application._command_to_csv(["unexpected"])
        application.busy = True
        application._command_to_excel([])
        application.busy = False
        application.results_open = False
        application._command_to_csv([])
        application.results_open = True
        application.session.export_state = lambda: (_ for _ in ()).throw(
            SqlExplorerConfigurationError("missing")
        )
        application._command_to_csv([])
        application.session.export_state = lambda: state

        application._command_to_excel([])
        screen, callback = application.screens.pop()
        assert isinstance(screen, NewFileScreen)
        callback(None)
        callback("result.xlsx")
        screen, callback = application.screens.pop()
        assert isinstance(screen, FileNavigationScreen)
        callback(None)

        outside = tmp_path.parent
        application._export_directory_selected("csv", "outside.csv", outside)
        directory = tmp_path / "directory.csv"
        directory.mkdir()
        application._export_directory_selected("csv", "directory.csv", tmp_path)
        existing = tmp_path / "existing.csv"
        existing.write_text("old", encoding="utf-8")
        application._export_directory_selected("csv", existing.name, tmp_path)
        screen, callback = application.screens.pop()
        assert isinstance(screen, ConfirmExportScreen)
        callback(False)
        application._export_directory_selected("csv", existing.name, tmp_path)
        _screen, callback = application.screens.pop()
        callback(True)
        application._export_directory_selected("csv", "new.csv", tmp_path)

        assert len(application.errors) == 5
        assert application.writes == [
            ("csv", existing),
            ("csv", tmp_path / "new.csv"),
        ]
        assert application.status_updates == 2

    asyncio.run(exercise())


def test_truncated_export_confirmation_rechecks_mutation_plan(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)

    async def exercise() -> None:
        select_plan = build_execution_plan("select value from sample", "gp")
        state = SimpleNamespace(plan=select_plan, dataframe=None, truncated=True)
        application = ExportHarness(state)

        application._command_to_csv([])
        screen, callback = application.screens.pop()
        assert isinstance(screen, ConfirmExportScreen)
        callback(False)
        callback(True)
        screen, _callback = application.screens.pop()
        assert isinstance(screen, NewFileScreen)

        mutation_plan = build_execution_plan("delete from sample", "gp")
        state.plan = mutation_plan
        application._full_export_confirmed("csv", True)
        screen, callback = application.screens.pop()
        assert isinstance(screen, ConfirmMutationScreen)
        callback(False)
        callback(True)
        screen, _callback = application.screens.pop()
        assert isinstance(screen, NewFileScreen)
        assert application.notices == ["Export cancelled.", "Export cancelled."]

    asyncio.run(exercise())


def test_export_callbacks_ignore_closed_tabs_and_finish_closing_tabs(tmp_path: Path) -> None:
    plan = build_execution_plan("select value from sample", "gp")
    state = SimpleNamespace(plan=plan, dataframe=pd.DataFrame({"value": [1]}), truncated=False)
    application = ExportHarness(state)

    application._full_export_confirmed("csv", True, "missing")
    application._export_directory_selected("csv", "result.csv", tmp_path, "missing")
    application._write_export("csv", tmp_path / "result.csv", "missing")
    application._finish_export(tmp_path / "result.csv", "missing")
    application._finish_export_error(RuntimeError("closed"), "missing")

    application.workspace.closing = True
    application._finish_export(tmp_path / "result.csv", "1")
    application._finish_export_error(RuntimeError("closing"), "1")

    assert application.removed == ["1", "1"]
    assert application.exit_checks == 2


def test_export_blocks_only_when_its_database_has_active_sql(tmp_path: Path) -> None:
    async def exercise() -> None:
        plan = build_execution_plan("select value from sample", "gp")
        state = SimpleNamespace(plan=plan, dataframe=pd.DataFrame({"value": [1]}), truncated=False)
        application = ExportHarness(state)
        busy_workspace = SimpleNamespace(
            busy=True,
            operation_database=None,
            session=SimpleNamespace(database=SimpleNamespace(connection_key="gp")),
        )
        application._workspaces["busy"] = busy_workspace
        application.workspace.session.database = SimpleNamespace(
            connection_key="lake",
            backend="trino",
        )

        application._start_export_command("csv", [])
        assert isinstance(application.screens[-1][0], NewFileScreen)

        application.workspace.session.database = SimpleNamespace(connection_key="gp", backend="gp")
        application._write_export("csv", tmp_path / "blocked.csv", "1")

        assert application.writes == []
        assert "on gp" in application.notices[-1]

    asyncio.run(exercise())
