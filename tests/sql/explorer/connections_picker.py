from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace
from typing import TYPE_CHECKING

from analytics_toolkit import general
from analytics_toolkit.sql_explorer import connections_commands, connections_picker
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.connections import ConnectionsRestart
from analytics_toolkit.sql_explorer.connections_picker import (
    ConnectionsPickerApp,
    ConnectionsPickerScreen,
)
from analytics_toolkit.sql_explorer.discovery import DiscoveryProgress
from textual.widgets import Input, OptionList, Static

from tests.sql.explorer.app import FakeSession

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_first_launch_waits_for_complete_scan_and_multiple_files_require_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(ConnectionsPickerScreen, "_scan", lambda self: None)
    first = tmp_path / ".connections"
    second = tmp_path / "other" / ".connections"
    second.parent.mkdir()
    second.write_text("{}", encoding="utf-8")

    async def scenario() -> None:
        app = ConnectionsPickerApp(auto_select=True)
        async with app.run_test(size=(120, 40)) as pilot:
            app.picker._show_progress(DiscoveryProgress((first,), 1))
            await pilot.pause()
            assert app.return_value is None
            app.picker._show_progress(DiscoveryProgress((first, second), 2, complete=True))
            await pilot.pause()
            assert app.picker.query_one(OptionList).option_count == 2
            await pilot.press("down", "enter")
            await pilot.pause()
            assert app.return_value == second

    asyncio.run(scenario())


def test_completed_single_match_is_selected_automatically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(ConnectionsPickerScreen, "_scan", lambda self: None)

    async def scenario() -> None:
        app = ConnectionsPickerApp(auto_select=True)
        async with app.run_test(size=(120, 40)) as pilot:
            app.picker._show_progress(
                DiscoveryProgress((tmp_path / ".connections",), complete=True)
            )
            await pilot.pause()
            assert app.return_value == tmp_path / ".connections"

    asyncio.run(scenario())


def test_empty_scan_manual_path_validation_and_cancel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(ConnectionsPickerScreen, "_scan", lambda self: None)

    async def scenario() -> None:
        app = ConnectionsPickerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            app.picker._show_progress(DiscoveryProgress((), complete=True))
            await pilot.press("ctrl+l")
            field = app.picker.query_one(Input)
            assert field.has_focus
            field.value = str(tmp_path / "missing")
            await pilot.press("enter")
            assert "existing .connections" in str(
                app.picker.query_one("#connections-progress", Static).render()
            )
            field.value = str(tmp_path / ".connections")
            await pilot.press("enter")
            await pilot.pause()
            assert app.return_value == tmp_path / ".connections"
        cancelled = ConnectionsPickerApp()
        async with cancelled.run_test(size=(120, 40)) as pilot:
            await pilot.press("escape")
            assert cancelled.picker.cancelled.is_set()

    asyncio.run(scenario())


def test_scanner_worker_delivers_progress_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            connections_picker,
            "discover_connections",
            lambda event: iter([DiscoveryProgress((), 12, complete=True)]),
        )
        app = ConnectionsPickerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert "No .connections" in str(
                app.picker.query_one("#connections-progress", Static).render()
            )

        def fail_scan(event: object) -> None:
            message = "mount enumeration unavailable"
            raise OSError(message)

        monkeypatch.setattr(connections_picker, "discover_connections", fail_scan)
        app = ConnectionsPickerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert "unavailable" in str(
                app.picker.query_one("#connections-progress", Static).render()
            )

    asyncio.run(scenario())


def test_connections_command_cancel_and_same_source_preserve_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(ConnectionsPickerScreen, "_scan", lambda self: None)
    general.set_connections_path(tmp_path / ".connections")

    async def scenario() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 40)) as pilot:
            app.active_workspace.editor.text = "select important_work"
            app._handle_command(["connections"])
            await pilot.pause()
            await pilot.press("escape")
            assert app.active_workspace.editor.text == "select important_work"
            app._handle_command(["connections"])
            await pilot.pause()
            await pilot.press("enter")
            assert app._connections_restart is None
            assert app.active_workspace.editor.text == "select important_work"

    asyncio.run(scenario())


def test_switch_dirty_cancel_and_discard_to_fresh_workspace(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = SqlExplorerApp(FakeSession())
        request = ConnectionsRestart(tmp_path / "new" / ".connections")
        async with app.run_test(size=(120, 40)) as pilot:
            app.active_workspace.editor.text = "select unsaved"
            app._request_exit(restart=request)
            await pilot.pause()
            await pilot.press("escape")
            assert app._connections_restart is None
            assert app.active_workspace.editor.text == "select unsaved"
            app._request_exit(restart=request)
            await pilot.pause()
            app.screen.dismiss("discard")
            await pilot.pause(0.3)
            assert app.return_value == request

    asyncio.run(scenario())


def test_switch_save_failure_keeps_source_and_tabs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def scenario() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 40)) as pilot:
            app.active_workspace.current_file = tmp_path / "query.sql"
            app.active_workspace.editor.text = "select unsaved"
            monkeypatch.setattr(app, "_save_workspace", lambda workspace: False)
            app._request_exit(restart=ConnectionsRestart(tmp_path / "new" / ".connections"))
            await pilot.pause()
            app.screen.dismiss("save")
            await pilot.pause()
            assert app._connections_restart is None
            assert not app._exit_requested
            assert app.active_workspace.editor.text == "select unsaved"

    asyncio.run(scenario())


def test_switch_command_selects_new_source_and_rejects_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ConnectionsPickerScreen, "_scan", lambda self: None)

    async def scenario() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 40)) as pilot:
            requested = []
            monkeypatch.setattr(
                app, "_request_exit", lambda **kwargs: requested.append(kwargs["restart"])
            )
            app._command_connections(["unexpected"])
            app._command_connections([])
            await pilot.pause()
            app._command_connections([])
            assert len(app.screen_stack) == 2
            app.screen.dismiss(tmp_path / ".connections")
            await pilot.pause()
            assert requested == [ConnectionsRestart(tmp_path / ".connections")]

    asyncio.run(scenario())


def test_switch_timeout_preserves_workspace_and_resumes_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:

    async def scenario() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 40)):
            original_pool = app._completion_pool
            resumed = []
            monkeypatch.setattr(
                app, "_completion_pool", SimpleNamespace(stop=lambda: None, is_stopped=False)
            )
            monkeypatch.setattr(app, "_start_completion_coordinator", resumed.append)
            monkeypatch.setattr(connections_commands, "monotonic", lambda: 100)
            app._connections_stop_started = 0
            app._connections_stop_timer = SimpleNamespace(stop=lambda: None)
            app._connections_restart = ConnectionsRestart(tmp_path / ".connections")
            app._exit_requested = True
            app._finish_connections_shutdown()
            assert not app._exit_requested
            assert app._connections_restart is None
            assert resumed == [app.active_workspace]
            monkeypatch.setattr(app, "_completion_pool", original_pool)

    asyncio.run(scenario())


def test_shutdown_rejects_new_work_and_waits_for_query_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        session = FakeSession()
        app = SqlExplorerApp(session)
        async with app.run_test(size=(120, 40)):
            workspace = app.active_workspace
            plan = session.plan("select 1")
            completed = []
            monkeypatch.setattr(app, "_finish_exit_if_ready", lambda: completed.append(True))
            app._exit_requested = True
            app._handle_command(["db", "other"])
            app._start_completion_coordinator()
            app._enqueue_query(workspace, plan, session.database)
            app._drain_query_queue()
            assert session.database.connection_key == "gp"
            assert not session.executed
            workspace.exit_after_cancel = True
            app._finish_result(session.execute(plan))
            assert completed == [True]
            app._exit_requested = False

    asyncio.run(scenario())


def test_picker_preserves_selection_across_progress_and_cancels_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:

    finished = Event()

    def scan(cancelled: Event):
        cancelled.set()
        yield DiscoveryProgress(())
        finished.set()

    monkeypatch.setattr(connections_picker, "discover_connections", scan)

    async def scenario() -> None:
        app = ConnectionsPickerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.picker.cancelled.is_set()
            assert not finished.is_set()
            first = tmp_path / ".connections"
            second = tmp_path / "other" / ".connections"
            app.picker._show_progress(DiscoveryProgress((first, second)))
            options = app.picker.query_one(OptionList)
            options.highlighted = 1
            app.picker._show_progress(DiscoveryProgress((second,), complete=True))
            assert options.highlighted == 0
            assert app.picker.paths == (second,)

    asyncio.run(scenario())


def test_cancelled_scan_error_does_not_update_closed_picker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def scan(cancelled: Event):
        cancelled.set()
        message = "cancelled scan"
        raise OSError(message)

    monkeypatch.setattr(connections_picker, "discover_connections", scan)

    async def scenario() -> None:
        app = ConnectionsPickerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.picker.cancelled.is_set()
            assert app.picker.initial_error == ""

    asyncio.run(scenario())


def test_unselected_progress_and_unidentified_option_are_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ConnectionsPickerScreen, "_scan", lambda self: None)

    async def scenario() -> None:
        app = ConnectionsPickerApp()
        async with app.run_test(size=(120, 40)):
            app.picker._show_progress(DiscoveryProgress((tmp_path / ".connections",)))
            options = app.picker.query_one(OptionList)
            options.highlighted = None
            app.picker._show_progress(DiscoveryProgress((tmp_path / "other" / ".connections",)))
            assert options.highlighted == 0
            options.add_option("Unidentified option")
            app.picker.on_option_list_option_selected(OptionList.OptionSelected(options, 1))
            assert not app.picker.cancelled.is_set()
            app.picker._show_error("Invalid configuration")
            app.picker._show_progress(DiscoveryProgress((), complete=True))
            assert options.option_count == 0
            assert "Invalid configuration" in str(
                app.picker.query_one("#connections-progress", Static).render()
            )

    asyncio.run(scenario())
