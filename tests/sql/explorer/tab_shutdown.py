from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.runtime import DatabaseSelection, ExplorerCancelResult

from tests.sql.explorer.app import FakeSession

if TYPE_CHECKING:
    import pytest


def test_cancel_pending_idle_running_and_closed_workspace_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            first = application.active_workspace
            application.action_new_tab()
            await pilot.pause()
            second = application.active_workspace
            plan = second.session.plan("select 1")
            application._query_scheduler.enqueue(
                second.tab_id,
                plan,
                DatabaseSelection("gp", "gp"),
            )
            second.query_state = "queued"
            application._request_cancel(workspace=second)
            assert second.tab_id in application._workspaces
            application._query_scheduler.enqueue(
                second.tab_id,
                plan,
                DatabaseSelection("gp", "gp"),
            )
            second.query_state = "queued"
            application._request_cancel(workspace=second, close_after=True)
            assert second.tab_id not in application._workspaces

            exit_checks: list[bool] = []
            monkeypatch.setattr(
                application,
                "_finish_exit_if_ready",
                lambda: exit_checks.append(True),
            )
            application._exit_requested = True
            application._request_cancel(workspace=first, exit_after=True)
            assert exit_checks == [True]

            application._exit_requested = False
            application.action_new_tab()
            await pilot.pause()
            idle_close = application.active_workspace
            application._request_cancel(workspace=idle_close, close_after=True)
            assert idle_close.tab_id not in application._workspaces

            cancellations: list[str] = []
            monkeypatch.setattr(
                application,
                "_cancel_in_worker",
                lambda tab_id, _session: cancellations.append(tab_id),
            )
            first.busy = True
            application._request_cancel(workspace=first)
            application._request_cancel(workspace=first, exit_after=True, close_after=True)
            assert cancellations == [first.tab_id]
            assert first.exit_after_cancel is True
            assert first.closing is True

            result = ExplorerCancelResult(1, 1, "cancelled")
            application._finish_cancel(result)
            application._finish_cancel_for("missing", result)
            application._finish_cancel_error_for("missing", RuntimeError("closed"))
            first.cancelling = False
            first.closing = False
            application._finish_cancel_error(RuntimeError("failed"))
            assert first.query_state == "running"

    asyncio.run(exercise())


def test_exit_shutdown_removes_pending_jobs_and_requests_busy_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            first = application.active_workspace
            application.action_new_tab()
            await pilot.pause()
            second = application.active_workspace
            pending = application._query_scheduler.enqueue(
                second.tab_id,
                second.session.plan("select 2"),
                DatabaseSelection("gp", "gp"),
            )
            assert pending is not None
            second.query_state = "queued"
            first.busy = True
            cancellations: list[tuple[str, bool]] = []
            monkeypatch.setattr(
                application,
                "_request_cancel",
                lambda *, workspace, exit_after: cancellations.append(
                    (workspace.tab_id, exit_after)
                ),
            )
            application._begin_exit_shutdown()

            assert second.query_state == "ready"
            assert cancellations == [(first.tab_id, True)]
            assert application._exit_requested is True

    asyncio.run(exercise())
