from __future__ import annotations

import asyncio
from dataclasses import replace
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest
from analytics_toolkit.sql_explorer import runtime
from analytics_toolkit.sql_explorer.app import ResultMessage, SqlExplorerApp
from analytics_toolkit.sql_explorer.runtime import (
    ExplorerQueryState,
    ExplorerRunResult,
    format_duration,
)
from analytics_toolkit.sql_explorer.statements import ExecutionRoute, build_execution_plan
from analytics_toolkit.sql_explorer.status import CircularSpinner, format_compact_duration
from textual.css.query import NoMatches
from textual.widgets import Button, Static

from tests.sql.explorer.app import FakeSession

if TYPE_CHECKING:
    from pathlib import Path


def _session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> runtime.ExplorerSession:
    valid = SimpleNamespace(connection_key="gp", backend="gp", valid=True, error=None)
    monkeypatch.setattr(
        runtime.sql,
        "validate_connections",
        lambda _keys, connect: [valid],
    )
    return runtime.ExplorerSession("gp", settings_path=tmp_path / "settings.json")


def test_duration_formatting_uses_readable_seconds_minutes_and_hours() -> None:
    assert format_duration(1) == "1 second"
    assert format_duration(2.9) == "2 seconds"
    assert format_duration(65) == "1 minute 5 seconds"
    assert format_duration(60) == "1 minute"
    assert format_duration(5_405) == "1 hour 30 minutes 5 seconds"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.128, "0.128s"),
        (6.0, "6s"),
        (12.34, "12.3s"),
        (65.9, "1m 05s"),
        (5_405.9, "1h 30m 05s"),
    ],
)
def test_compact_duration_adapts_to_elapsed_time(seconds: float, expected: str) -> None:
    assert format_compact_duration(seconds) == expected


def test_session_retains_completed_and_failed_query_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime.sql, "read", lambda *_args, **_kwargs: pd.DataFrame({"x": [1]}))

    result = session.execute(build_execution_plan("select 1", "gp"))

    assert result.dataframe is not None
    assert session.active_query is None
    assert session.active_query_label is None
    assert session.last_query is not None
    assert session.last_query.state == "completed"
    assert session.last_query.route is ExecutionRoute.READ
    completed_label = session.last_query.label

    monkeypatch.setattr(
        runtime.sql,
        "read",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("failed")),
    )
    try:
        session.execute(build_execution_plan("select 2", "gp"))
    except RuntimeError:
        pass
    else:  # pragma: no cover - protects the state assertion below.
        message = "expected SQL failure"
        raise AssertionError(message)

    assert session.last_query is not None
    assert session.last_query.label != completed_label
    assert session.last_query.state == "failed"


def test_cancelled_query_keeps_label_and_uses_plural_sql_helpers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session(monkeypatch, tmp_path)
    started = Event()
    release = Event()
    cancelled = Event()
    error: list[BaseException] = []
    helper_calls: list[str] = []

    def fake_read(*_args: Any, **_kwargs: Any) -> pd.DataFrame:
        started.set()
        release.wait(2)
        if cancelled.is_set():
            message = "cancelled by backend"
            raise RuntimeError(message)
        return pd.DataFrame({"x": [1]})

    def fake_show_queries(*_args: Any, **_kwargs: Any) -> pd.DataFrame:
        helper_calls.append("show_queries")
        assert session.active_query_label is not None
        return pd.DataFrame({"query_id": [17], "query": [f"label={session.active_query_label}"]})

    def fake_cancel_queries(*_args: Any, **_kwargs: Any) -> pd.DataFrame:
        helper_calls.append("cancel_queries")
        cancelled.set()
        return pd.DataFrame({"cancelled": [True]})

    monkeypatch.setattr(runtime.sql, "read", fake_read)
    monkeypatch.setattr(runtime.sql, "show_queries", fake_show_queries)
    monkeypatch.setattr(runtime.sql, "cancel_queries", fake_cancel_queries)

    def execute() -> None:
        try:
            session.execute(build_execution_plan("select 1", "gp"))
        except BaseException as exc:  # noqa: BLE001 - captured across a test thread.
            error.append(exc)

    worker = Thread(target=execute)
    worker.start()
    assert started.wait(2)
    active_label = session.active_query_label
    assert active_label is not None

    cancel_result = session.cancel_active()
    assert cancel_result.cancelled_queries == 1
    assert helper_calls == ["show_queries", "cancel_queries"]
    assert session.active_query is not None
    assert session.active_query.state == "cancelling"

    release.set()
    worker.join(2)
    assert error
    assert "cancelled" in str(error[0])
    assert session.last_query is not None
    assert session.last_query.label == active_label
    assert session.last_query.state == "cancelled"
    assert session.active_query_label is None


def test_query_cleanup_and_cancel_failures_remain_label_targeted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session(monkeypatch, tmp_path)
    replacement = ExplorerQueryState(
        "sql_explorer run=replacement",
        ExecutionRoute.READ,
        1.0,
        None,
        "running",
    )

    def replace_active(*_args: Any, **_kwargs: Any) -> pd.DataFrame:
        session.active_query_label = replacement.label
        session.active_query = replacement
        return pd.DataFrame({"x": [1]})

    monkeypatch.setattr(runtime.sql, "read", replace_active)
    session.execute(build_execution_plan("select 1", "gp"))
    assert session.active_query_label == replacement.label
    assert session.active_query is replacement

    monkeypatch.setattr(runtime, "_CANCEL_LOOKUP_ATTEMPTS", 1)
    monkeypatch.setattr(
        runtime.sql,
        "show_queries",
        lambda *_args, **_kwargs: pd.DataFrame(columns=["query_id", "query"]),
    )
    result = session.cancel_active()
    assert result.cancelled_queries == 0
    assert session.active_query is not None
    assert session.active_query.state == "running"

    monkeypatch.setattr(
        runtime.sql,
        "show_queries",
        lambda *_args, **_kwargs: pd.DataFrame({"query_id": [9], "query": [replacement.label]}),
    )
    monkeypatch.setattr(
        runtime.sql,
        "cancel_queries",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cancel failed")),
    )
    with pytest.raises(RuntimeError, match="cancel failed"):
        session.cancel_active()
    assert session.active_query is not None
    assert session.active_query.state == "running"
    assert session._cancellation_requested_for is None

    def replace_then_fail(*_args: Any, **_kwargs: Any) -> pd.DataFrame:
        session.active_query = replace(replacement, label="different")
        message = "cancel failed after replacement"
        raise RuntimeError(message)

    monkeypatch.setattr(runtime.sql, "cancel_queries", replace_then_fail)
    with pytest.raises(RuntimeError, match="after replacement"):
        session.cancel_active()
    assert session.active_query.label == "different"


def test_running_elapsed_shows_tenths_and_refreshes_between_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [10.128]
    monkeypatch.setattr(runtime, "monotonic", lambda: now[0])

    async def exercise() -> None:
        session = FakeSession()
        session.active_query = ExplorerQueryState(
            "running", ExecutionRoute.READ, 10.0, None, "running"
        )
        application = SqlExplorerApp(session)
        async with application.run_test() as pilot:
            application.active_workspace.query_state = "running"
            application._update_status()
            elapsed = application.query_one("#query-elapsed", Static)
            assert str(elapsed.renderable) == "0.1s"
            for seconds, expected in ((10.245, "0.2s"), (10.36, "0.4s"), (22.3, "12.3s")):
                now[0] = seconds
                await pilot.pause(0.15)
                assert str(elapsed.renderable) == expected

    asyncio.run(exercise())


def test_running_summary_animates_and_keeps_slow_warning() -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.active_query = ExplorerQueryState(
            "sql_explorer run=active",
            ExecutionRoute.READ,
            monotonic() - 301,
            None,
            "running",
        )
        application = SqlExplorerApp(session)
        async with application.run_test() as pilot:
            application.active_workspace.query_state = "running"
            application.busy = True
            application._update_status()
            indicator = application.query_one("#query-running-indicator", CircularSpinner)
            assert indicator.display
            first_frame = indicator.renderable
            await pilot.pause(CircularSpinner.INTERVAL_SECONDS * 1.5)
            assert indicator.renderable != first_frame
            assert "Query running" in str(application.query_one("#query-outcome").render())
            assert "consider optimizing" in str(application.query_one("#query-warning").render())
            interrupt = application.query_one("#interrupt", Button)
            assert str(interrupt.label) == "STOP"
            assert interrupt.disabled is False
            outcome = application.query_one("#query-outcome")
            assert indicator.region.y == outcome.region.y
            assert indicator.region.bottom == outcome.region.bottom
            assert indicator.region.width == 5
            assert all(indicator.render_line(row).text.strip() for row in range(3))

            application.active_workspace.query_state = "ready"
            application.busy = False
            application._update_status()
            assert indicator.display is False
            assert indicator.renderable == CircularSpinner.FRAMES[0]

    asyncio.run(exercise())


def test_completed_summary_keeps_rows_and_elapsed_after_results_close() -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.last_query = ExplorerQueryState(
            "sql_explorer run=completed",
            ExecutionRoute.READ,
            10.0,
            10.128,
            "completed",
        )
        application = SqlExplorerApp(session)
        async with application.run_test():
            result = ExplorerRunResult(
                route=ExecutionRoute.READ,
                dataframe=pd.DataFrame({"value": range(200)}),
                displayed_rows=200,
                total_rows=1_234,
                truncated=True,
                status="Showing the first 200 of 1,234 rows.",
            )
            application._render_result(result, application.active_workspace)
            application.close_results()
            application._update_status()

            assert "Query succeeded" in str(application.query_one("#query-outcome").render())
            assert "200 of 1,234 rows" in str(application.query_one("#query-rows").render())
            assert "0.128s" in str(application.query_one("#query-elapsed").render())
            assert application.query_one("#interrupt", Button).disabled is True
            assert str(application.query_one("#notice", Static).renderable) == ""

    asyncio.run(exercise())


def test_query_summary_hides_cards_for_queue_and_formats_unknown_truncation() -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.last_query = ExplorerQueryState(
            "sql_explorer run=completed",
            ExecutionRoute.READ,
            0.0,
            65.0,
            "completed",
        )
        application = SqlExplorerApp(session)
        async with application.run_test():
            workspace = application.active_workspace
            workspace.last_run_result = ExplorerRunResult(
                route=ExecutionRoute.READ,
                dataframe=pd.DataFrame({"value": range(200)}),
                displayed_rows=200,
                total_rows=None,
                truncated=True,
                status="Showing the first 200 rows; more rows are available.",
            )
            application._update_status()
            assert "200+ rows" in str(application.query_one("#query-rows").render())

            workspace.last_run_result = ExplorerRunResult(
                route=ExecutionRoute.READ,
                dataframe=pd.DataFrame({"value": [1]}),
                displayed_rows=1,
                total_rows=1,
                truncated=False,
                status="1 row.",
            )
            application._update_status()
            assert "1 row" in str(application.query_one("#query-rows").render())

            workspace.last_run_result = ExplorerRunResult(
                route=ExecutionRoute.READ,
                dataframe=pd.DataFrame({"value": [1, 2]}),
                displayed_rows=2,
                total_rows=2,
                truncated=False,
                status="2 rows.",
            )
            application._update_status()
            assert "2 rows" in str(application.query_one("#query-rows").render())

            workspace.query_state = "queued"
            application._update_status()
            assert application.query_one("#query-outcome").display is False
            assert application.query_one("#query-rows").display is False
            assert application.query_one("#query-elapsed").display is False

    asyncio.run(exercise())


def test_interrupt_stays_disabled_until_query_worker_acknowledges() -> None:
    async def exercise() -> None:
        session = FakeSession()
        application = SqlExplorerApp(session)
        async with application.run_test() as pilot:
            application.busy = True
            application._update_status()
            interrupt = application.query_one("#interrupt", Button)
            assert interrupt.disabled is False

            await pilot.click("#interrupt")
            await pilot.pause()
            assert session.cancel_calls == 1
            assert application.busy is True
            assert application.cancelling is True
            assert interrupt.disabled is True

            application._finish_error(RuntimeError("backend cancellation"))
            assert application.busy is False
            assert application.cancelling is False
            assert isinstance(application.query_one(ResultMessage), ResultMessage)

    asyncio.run(exercise())


def test_metadata_notice_remains_without_the_removed_verbose_status() -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.last_query = ExplorerQueryState(
            "sql_explorer run=user-query",
            ExecutionRoute.EXECUTE,
            10.0,
            12.0,
            "failed",
        )
        application = SqlExplorerApp(session)
        async with application.run_test():
            application._set_notice("Metadata completion unavailable: permission denied")
            application._update_status()
            notice = str(application.query_one("#notice", Static).render())
            assert not list(application.query("#session-status"))
            assert "Query failed" in str(application.query_one("#query-outcome").render())
            assert "Metadata completion unavailable" in notice

    asyncio.run(exercise())


def test_finish_exit_and_non_completion_event_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        session = FakeSession()
        application = SqlExplorerApp(session)
        async with application.run_test():
            exits: list[bool] = []
            monkeypatch.setattr(application, "exit", lambda: exits.append(True))
            plan = build_execution_plan("select 1", "gp")
            result = session.execute(plan)

            application._finish_result(result)
            application._exit_after_cancel = True
            application._finish_result(result)
            application._exit_after_cancel = True
            application._finish_error(RuntimeError("cancelled"))
            assert exits == [True, True]

            application.on_option_list_option_selected(SimpleNamespace(option_list=object()))
            application._command_open(["unexpected"])
            assert "Usage: open" in str(application.query_one(ResultMessage).render())

            assert application._completion is not None
            application._completion.stop()
            application._completion = None

    asyncio.run(exercise())


def test_status_updates_ignore_unavailable_workspace_widgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        application._update_status()
        application._set_notice("not mounted")
        async with application.run_test():
            workspace = application.active_workspace

            def missing(*_args: object, **_kwargs: object) -> None:
                message = "remounting"
                raise NoMatches(message)

            with monkeypatch.context() as patch:
                patch.setattr(workspace, "query_one", missing)
                application._update_status()
                application._update_editor_status()
                application._set_notice("remounting")

    asyncio.run(exercise())
