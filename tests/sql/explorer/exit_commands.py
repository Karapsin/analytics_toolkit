from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.file_commands import NewSqlFileScreen
from analytics_toolkit.sql_explorer.tabs import SaveChangesScreen

from tests.sql.explorer.app import FakeSession

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("command", ["exit!", "q!"])
def test_force_exit_skips_dirty_dialog_and_does_not_save(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "query.sql"
    path.write_text("select 1")

    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        exits = []
        async with app.run_test() as pilot:
            monkeypatch.setattr(app, "exit", lambda: exits.append(True))
            app.load_sql_file(path)
            app.active_workspace.editor.text = "select 2"
            app._handle_command([command])
            await pilot.pause()
            assert exits == [True]
            assert len(app.screen_stack) == 1
            assert path.read_text() == "select 1"

    asyncio.run(exercise())


def test_q_keeps_normal_unsaved_changes_dialog() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            app.active_workspace.editor.text = "select 1"
            app._handle_command(["q"])
            await pilot.pause()
            assert isinstance(app.screen, SaveChangesScreen)
            await pilot.press("escape")
            assert not app._exit_requested

    asyncio.run(exercise())


def test_wq_saves_all_changed_tabs_before_exiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [tmp_path / "one.sql", tmp_path / "two.sql"]
    for path in paths:
        path.write_text("old")

    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        exits = []
        async with app.run_test() as pilot:
            monkeypatch.setattr(app, "exit", lambda: exits.append(True))
            app.load_sql_file(paths[0])
            app.active_workspace.editor.text = "select 1"
            app.action_new_tab()
            await pilot.pause()
            app.load_sql_file(paths[1])
            app.active_workspace.editor.text = "select 2"
            app._handle_command(["wq"])
            await pilot.pause()
            assert exits == [True]
            assert [path.read_text() for path in paths] == ["select 1", "select 2"]
            assert len(app.screen_stack) == 1

    asyncio.run(exercise())


def test_wq_untitled_cancel_and_save_failure_keep_app_open(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            app.active_workspace.editor.text = "select 1"
            app._handle_command(["wq"])
            await pilot.pause()
            assert isinstance(app.screen, NewSqlFileScreen)
            await pilot.press("escape")
            assert not app._exit_requested
            app.active_workspace.current_file = tmp_path / "missing.sql"
            app._handle_command(["wq"])
            assert not app._exit_requested
            assert "no longer exists" in str(app.active_workspace.result_message.render())
            assert app.active_workspace.is_dirty

    asyncio.run(exercise())


@pytest.mark.parametrize("command", ["q", "q!", "exit!", "wq"])
def test_exit_aliases_reject_extra_arguments(command: str) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            app._handle_command([command, "unexpected"])
            assert not app._exit_requested
            assert "Usage:" in str(app.active_workspace.result_message.render())

    asyncio.run(exercise())


def test_force_exit_waits_for_running_query_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise() -> None:
        session = FakeSession()
        app = SqlExplorerApp(session)
        exits = []
        async with app.run_test() as pilot:
            monkeypatch.setattr(app, "exit", lambda: exits.append(True))
            workspace = app.active_workspace
            workspace.editor.text = "unsaved"
            workspace.busy = True
            workspace.query_state = "running"
            app._handle_command(["q!"])
            await pilot.pause()
            assert session.cancel_calls == 1
            assert exits == []
            assert len(app.screen_stack) == 1
            app._finish_error(RuntimeError("cancelled"))
            assert exits == [True]

    asyncio.run(exercise())
