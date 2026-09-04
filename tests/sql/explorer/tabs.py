from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pandas as pd
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.file_commands import NewSqlFileScreen
from analytics_toolkit.sql_explorer.runtime import (
    DatabaseSelection,
    ExplorerRunResult,
)
from analytics_toolkit.sql_explorer.tabs import SaveChangesScreen, WorkspaceTab
from analytics_toolkit.sql_explorer.widgets import FileNavigationScreen
from textual.document._document import Selection

from tests.sql.explorer.app import FakeSession

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_tabs_keep_complete_workspace_state_and_wrap_keyboard_switching() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(120, 40)) as pilot:
            first = application.active_workspace
            first.editor.text = "select 1"
            first.command_input.value = "first command"
            application.show_dataframe(pd.DataFrame({"first": [1]}), first)

            await pilot.press("ctrl+t")
            await pilot.pause()
            second = application.active_workspace
            assert second is not first
            assert second.session.database.connection_key == "gp"
            assert second.editor.text == ""
            assert second.command_input.value == ""
            assert second.results_open is False

            second.editor.text = "select 2"
            second.command_input.value = "second command"
            application.show_message("second result", second)
            await pilot.press("ctrl+tab")
            assert application.active_workspace is first
            assert first.editor.text == "select 1"
            assert first.command_input.value == "first command"
            assert first.results_open is True
            assert first.result_table.row_count == 1

            await pilot.press("ctrl+shift+tab")
            assert application.active_workspace is second
            assert second.editor.text == "select 2"
            assert second.command_input.value == "second command"
            assert "second result" in str(second.result_message.render())

    asyncio.run(exercise())


def test_tab_labels_show_database_filename_dirty_state_and_click_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    sql_file = tmp_path / "query.sql"
    sql_file.write_text("select 1", encoding="utf-8")

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(120, 40)) as pilot:
            first = application.active_workspace
            assert first.tab_title == "[gp] Untitled 1"
            first.editor.text = "select changed"
            await pilot.pause()
            assert first.tab_title == "[gp] Untitled 1*"

            application.load_sql_file(sql_file)
            await pilot.pause()
            assert application.active_workspace.current_file == sql_file.resolve()
            assert application.active_workspace.tab_title == "[gp] query.sql"

            application._command_database(["lake"])
            assert application.active_workspace.tab_title == "[lake] query.sql"

            await pilot.click("#new-tab")
            await pilot.pause()
            assert len(application.query(WorkspaceTab)) == 3
            await pilot.click("#tab-1 .tab-select")
            assert application._active_tab_id == "1"
            application._activate_tab(application._tab_order[-1])
            active_id = application._active_tab_id
            await pilot.click(f"#tab-{active_id} .tab-close")
            await pilot.pause()
            assert active_id not in application._workspaces

    asyncio.run(exercise())


def test_tab_modal_actions_and_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise() -> None:
        save_screen = SaveChangesScreen("query.sql")
        save_decisions: list[object] = []
        focus_moves: list[str] = []
        monkeypatch.setattr(save_screen, "dismiss", save_decisions.append)
        monkeypatch.setattr(save_screen, "focus_previous", lambda: focus_moves.append("previous"))
        monkeypatch.setattr(save_screen, "focus_next", lambda: focus_moves.append("next"))

        save_screen.action_focus_previous()
        save_screen.action_focus_next()
        save_screen.action_save()
        save_screen.action_discard()
        save_screen.action_cancel()
        for button_id in (
            "save-changes-save",
            "save-changes-discard",
            "save-changes-cancel",
            "other",
        ):
            save_screen.on_button_pressed(
                SimpleNamespace(button=SimpleNamespace(id=button_id))  # type: ignore[arg-type]
            )

        assert focus_moves == ["previous", "next"]
        assert save_decisions == ["save", "discard", "cancel", "save", "discard", "cancel"]

    asyncio.run(exercise())


def test_dirty_tab_close_prompts_and_last_tab_is_protected() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            closing_id = application._active_tab_id
            application.active_workspace.editor.text = "select changed"

            await pilot.press("ctrl+w")
            assert isinstance(application.screen, SaveChangesScreen)
            await pilot.press("escape")
            assert closing_id in application._workspaces

            await pilot.press("ctrl+w", "d")
            await pilot.pause()
            assert closing_id not in application._workspaces
            assert len(application._workspaces) == 1

            final_id = application._active_tab_id
            await pilot.press("ctrl+w")
            assert final_id in application._workspaces
            assert "final" in str(application.active_workspace.query_one("#notice").render())

    asyncio.run(exercise())


def test_forwarded_command_shortcuts_control_tabs() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            first = application.active_workspace
            await pilot.press("meta+t")
            await pilot.pause()
            second = application.active_workspace
            assert second is not first

            await pilot.press("meta+shift+tab")
            assert application.active_workspace is first
            await pilot.press("meta+tab")
            assert application.active_workspace is second
            await pilot.press("meta+w")
            await pilot.pause()
            assert application.active_workspace is first

    asyncio.run(exercise())


def test_save_untitled_buffer_creates_file_in_same_tab_without_losing_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            workspace = application.active_workspace
            workspace.editor.text = "select 1\nfrom sample"
            workspace.editor.selection = Selection((0, 0), (1, len("from sample")))
            expected_selection = workspace.editor.selection

            await pilot.press("ctrl+s")
            screen = application.screen
            assert isinstance(screen, NewSqlFileScreen)
            screen.query_one("#new-file-name").value = "saved.sql"
            screen.action_confirm()
            await pilot.pause()
            picker = application.screen
            assert isinstance(picker, FileNavigationScreen)
            picker.action_choose_directory()
            await pilot.pause()

            path = tmp_path / "saved.sql"
            assert path.read_text(encoding="utf-8") == "select 1\nfrom sample"
            assert application.active_workspace is workspace
            assert workspace.current_file == path.resolve()
            assert workspace.editor.text == "select 1\nfrom sample"
            assert workspace.editor.selection == expected_selection
            assert workspace.is_dirty is False

    asyncio.run(exercise())


def test_new_file_keeps_dirty_source_tab_and_opens_blank_file_in_new_tab(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            source = application.active_workspace
            source.editor.text = "select source"

            await pilot.press("ctrl+n")
            screen = application.screen
            assert isinstance(screen, NewSqlFileScreen)
            screen.query_one("#new-file-name").value = "blank.sql"
            screen.action_confirm()
            await pilot.pause()
            picker = application.screen
            assert isinstance(picker, FileNavigationScreen)
            picker.action_choose_directory()
            await pilot.pause()

            assert source.editor.text == "select source"
            assert source.is_dirty is True
            assert application.active_workspace is not source
            assert application.active_workspace.editor.text == ""
            assert application.active_workspace.current_file == (tmp_path / "blank.sql").resolve()
            assert (tmp_path / "blank.sql").read_text(encoding="utf-8") == ""

    asyncio.run(exercise())


def test_query_queue_routes_snapshot_and_results_to_the_originating_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        started: list[tuple[Any, Any]] = []
        monkeypatch.setattr(
            application,
            "_execute_in_worker",
            lambda job, session: started.append((job, session)),
        )
        async with application.run_test() as pilot:
            first = application.active_workspace
            first.editor.text = "select 1"
            application.action_run_query()
            assert len(started) == 1

            application.action_new_tab()
            await pilot.pause()
            second = application.active_workspace
            second.editor.text = "select 2"
            application.action_run_query()
            assert len(started) == 1
            assert second.query_state == "queued"

            second.session.database = SimpleNamespace(connection_key="other", backend="trino")
            first_job = started[0][0]
            first_result = ExplorerRunResult(
                first_job.plan.route,
                pd.DataFrame({"first": [1]}),
                1,
                1,
                False,
                "first finished",
            )
            application._finish_query_job(first_job, first_result, None)

            assert len(started) == 2
            assert started[1][0].database.connection_key == "gp"
            assert first.results_open is True
            assert first.result_table.row_count == 1
            assert second.results_open is False

    asyncio.run(exercise())


def test_query_scheduler_runs_different_databases_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        started: list[Any] = []
        monkeypatch.setattr(
            application,
            "_execute_in_worker",
            lambda job, _session: started.append(job),
        )
        async with application.run_test() as pilot:
            application.active_workspace.editor.text = "select 1"
            application.action_run_query()
            assert application._database_is_busy("GP") is True
            assert application._database_is_busy("lake") is False
            application.action_new_tab()
            await pilot.pause()
            second = application.active_workspace
            second.session.database = SimpleNamespace(connection_key="lake", backend="trino")
            second.editor.text = "select 2"
            application.action_run_query()

            assert [job.database.connection_key for job in started] == ["gp", "lake"]
            assert application._query_scheduler.active_count == 2
            assert application._database_is_busy("lake") is True

    asyncio.run(exercise())


def test_running_dirty_tab_closes_only_after_cancel_and_query_worker_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        started: list[Any] = []
        cancellations: list[str] = []
        monkeypatch.setattr(
            application,
            "_execute_in_worker",
            lambda job, _session: started.append(job),
        )
        monkeypatch.setattr(
            application,
            "_cancel_in_worker",
            lambda tab_id, _session: cancellations.append(tab_id),
        )
        async with application.run_test() as pilot:
            first = application.active_workspace
            first.editor.text = "select 1"
            application.action_run_query()
            application.action_new_tab()
            await pilot.pause()

            application._request_close_tab(first.tab_id)
            assert isinstance(application.screen, SaveChangesScreen)
            await pilot.press("d")
            assert cancellations == [first.tab_id]
            assert first.tab_id in application._workspaces

            application._finish_cancel_error_for(first.tab_id, RuntimeError("cancel failed"))
            assert first.tab_id in application._workspaces
            assert first.closing is False

            application._request_close_tab(first.tab_id)
            await pilot.press("d")
            assert cancellations == [first.tab_id, first.tab_id]
            application._finish_query_job(started[0], None, RuntimeError("cancelled"))
            await pilot.pause()
            assert first.tab_id not in application._workspaces

    asyncio.run(exercise())


def test_framework_quit_checks_every_dirty_tab_before_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        exits: list[bool] = []
        monkeypatch.setattr(application, "exit", lambda: exits.append(True))
        async with application.run_test() as pilot:
            first = application.active_workspace
            first.editor.text = "first"
            application.action_new_tab()
            await pilot.pause()
            application.active_workspace.editor.text = "second"

            await application.action_quit()
            assert isinstance(application.screen, SaveChangesScreen)
            await pilot.press("d")
            assert isinstance(application.screen, SaveChangesScreen)
            await pilot.press("d")
            assert exits == [True]

    asyncio.run(exercise())


def test_close_saves_existing_and_untitled_tabs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = tmp_path / "existing.sql"
    existing.write_text("select old", encoding="utf-8")

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            application._switch_tab(1)
            application._finish_added_workspace("missing")
            application._activate_tab("missing")
            application._active_tab_id = "missing"
            application._activate_tab("1")
            application._request_close_tab("missing")
            application._remove_workspace("missing")

            first = application.active_workspace
            application.load_sql_file(existing)
            first.editor.text = "select saved"
            application.action_new_tab()
            await pilot.pause()
            application._request_close_tab(first.tab_id)
            assert isinstance(application.screen, SaveChangesScreen)
            await pilot.press("s")
            await pilot.pause()
            assert first.tab_id not in application._workspaces
            assert existing.read_text(encoding="utf-8") == "select saved"

            untitled = application.active_workspace
            untitled.editor.text = "select untitled"
            application.action_new_tab()
            await pilot.pause()
            created: list[tuple[str, str]] = []

            def create_and_continue(
                workspace: Any,
                *,
                text: str,
                after_create: Any,
                **_kwargs: object,
            ) -> None:
                created.append((workspace.tab_id, text))
                after_create(True)

            monkeypatch.setattr(application, "_start_new_sql_file", create_and_continue)
            application._close_decision(untitled.tab_id, "save")
            assert created == [(untitled.tab_id, "select untitled")]
            assert untitled.tab_id not in application._workspaces

            remaining = application.active_workspace
            application._close_decision(remaining.tab_id, "cancel")
            application._close_decision("missing", "save")

    asyncio.run(exercise())


def test_close_save_failure_and_pending_query_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        started: list[Any] = []
        monkeypatch.setattr(
            application, "_execute_in_worker", lambda job, _session: started.append(job)
        )
        async with application.run_test() as pilot:
            first = application.active_workspace
            first.current_file = tmp_path / "missing.sql"
            first.editor.text = "dirty"
            application.action_new_tab()
            await pilot.pause()
            application._close_decision(first.tab_id, "save")
            assert first.tab_id in application._workspaces

            active = application.active_workspace
            active.editor.text = "select 1"
            application.action_run_query()
            application.action_new_tab()
            await pilot.pause()
            queued = application.active_workspace
            queued.editor.text = "select 2"
            application.action_run_query()
            assert queued.query_state == "queued"
            application._finish_close_request(queued)
            await pilot.pause()
            assert queued.tab_id not in application._workspaces
            assert application._query_scheduler.pending_count == 0

            application._finish_query_job(started[0], None, RuntimeError("done"))

    asyncio.run(exercise())


def test_tab_switching_preserves_find_navigation_state() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            first = application.active_workspace
            application.action_open_find()
            assert application._find_navigation_bound is True
            application.action_new_tab()
            await pilot.pause()
            assert application.active_workspace is not first
            assert application._find_navigation_bound is False
            application._activate_tab(first.tab_id)
            assert application._find_navigation_bound is True

            modal = NewSqlFileScreen()
            await application.push_screen(modal)
            count = len(application._tab_order)
            application.action_new_tab()
            application.action_close_tab()
            application.action_next_tab()
            application._request_exit()
            assert len(application._tab_order) == count
            modal.dismiss(None)
            application._exit_requested = True
            application._request_exit()
            application._exit_requested = False

    asyncio.run(exercise())


def test_exit_prompt_skips_disappeared_workspaces(monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        exits: list[bool] = []
        monkeypatch.setattr(application, "exit", lambda: exits.append(True))
        async with application.run_test():
            application._exit_dirty_tabs = ["missing"]
            application._prompt_next_exit_dirty_tab()
            assert exits == [True]

    asyncio.run(exercise())


def test_query_queue_duplicate_blocking_missing_owner_and_compatibility_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        started: list[Any] = []
        monkeypatch.setattr(
            application, "_execute_in_worker", lambda job, _session: started.append(job)
        )
        async with application.run_test() as pilot:
            workspace = application.active_workspace
            workspace.editor.text = "select 1"
            application.action_run_query()
            application.action_run_query()
            assert len(started) == 1
            assert "already" in str(workspace.query_one("#notice").render())
            application._enqueue_query(
                workspace,
                started[0].plan,
                started[0].database,
            )

            application._finish_query_job(started[0], None, None)
            application._finish_query_job(started[0], None, None)
            plan = workspace.session.plan("select 2")
            workspace.busy = True
            application._start_execution(plan)
            assert workspace.query_state == "queued"
            workspace.busy = False
            application._drain_query_queue()
            assert len(started) == 2
            application._finish_query_job(started[1], None, None)

            missing = application._query_scheduler.enqueue(
                "missing",
                plan,
                DatabaseSelection("gp", "gp"),
            )
            assert missing is not None
            application._drain_query_queue()
            assert application._query_scheduler.job_for_tab("missing") is None
            application._receive_metadata_error("missing", RuntimeError("closed"))

            detached = application._query_scheduler.enqueue(
                "detached",
                plan,
                DatabaseSelection("gp", "gp"),
            )
            assert detached is not None
            assert application._query_scheduler.take_startable() == (detached,)
            application._finish_query_job(detached, None, None)

            modal = NewSqlFileScreen()
            await application.push_screen(modal)
            application.action_run_query()
            modal.dismiss(None)
            await pilot.pause()

    asyncio.run(exercise())


def test_exit_dirty_decisions_cover_cancel_save_failure_and_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = tmp_path / "existing.sql"
    existing.write_text("old", encoding="utf-8")

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            workspace = application.active_workspace
            application.action_new_tab()
            await pilot.pause()
            prompts: list[bool] = []
            monkeypatch.setattr(
                application,
                "_prompt_next_exit_dirty_tab",
                lambda: prompts.append(True),
            )

            application._exit_dirty_tabs = ["missing"]
            application._exit_dirty_decision("missing", "discard")

            application._exit_dirty_tabs = [workspace.tab_id]
            application._exit_dirty_decision(workspace.tab_id, "cancel")
            assert application._exit_dirty_tabs == []

            workspace.current_file = existing
            workspace.saved_text = "old"
            workspace.editor.text = "saved"
            application._exit_dirty_tabs = [workspace.tab_id]
            application._exit_dirty_decision(workspace.tab_id, "save")
            assert existing.read_text(encoding="utf-8") == "saved"

            workspace.current_file = tmp_path / "missing.sql"
            workspace.saved_text = "old"
            workspace.editor.text = "changed"
            application._exit_dirty_tabs = [workspace.tab_id]
            application._exit_dirty_decision(workspace.tab_id, "save")
            assert application._exit_dirty_tabs == []

            workspace.current_file = None
            callbacks: list[Any] = []

            def capture_create(*_args: object, after_create: Any, **_kwargs: object) -> None:
                callbacks.append(after_create)

            monkeypatch.setattr(application, "_start_new_sql_file", capture_create)
            application._exit_dirty_tabs = [workspace.tab_id]
            application._exit_dirty_decision(workspace.tab_id, "save")
            callbacks[-1](False)
            assert application._exit_dirty_tabs == []
            application._exit_dirty_tabs = [workspace.tab_id]
            callbacks[-1](True)
            assert application._exit_dirty_tabs == []
            application._exit_dirty_tabs = [workspace.tab_id]
            application._exit_save_finished("other", True)

            application._exit_dirty_tabs = [workspace.tab_id]
            application._exit_dirty_decision(workspace.tab_id, "discard")
            assert prompts

    asyncio.run(exercise())
