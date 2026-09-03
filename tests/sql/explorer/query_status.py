from __future__ import annotations

import asyncio
from dataclasses import replace
from threading import Event, Thread
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest
from analytics_toolkit.sql_explorer import runtime
from analytics_toolkit.sql_explorer.app import ResultMessage, SqlExplorerApp
from analytics_toolkit.sql_explorer.runtime import ExplorerQueryState, format_duration
from analytics_toolkit.sql_explorer.statements import ExecutionRoute, build_execution_plan
from rich.text import Text
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


def test_status_keeps_query_after_results_close_and_styles_slow_warning() -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.last_query = ExplorerQueryState(
            "sql_explorer run=kept",
            ExecutionRoute.READ,
            0.0,
            301.0,
            "completed",
        )
        application = SqlExplorerApp(session)
        async with application.run_test():
            application.show_message("result")
            application.close_results()
            application._update_status()
            status = application.query_one("#session-status", Static).render()
            status = getattr(status, "_renderable", status)

            assert isinstance(status, Text)
            assert "sql_explorer run=kept" in status.plain
            assert "route=sql.read" in status.plain
            assert "consider optimizing your query or sit tight" in status.plain
            warning_spans = [
                span
                for span in status.spans
                if "consider optimizing" in status.plain[span.start : span.end]
            ]
            assert warning_spans
            assert warning_spans[0].style == "bold red"

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


def test_metadata_notice_does_not_replace_user_query_status() -> None:
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
            status = str(application.query_one("#session-status", Static).render())
            notice = str(application.query_one("#notice", Static).render())
            assert "sql_explorer run=user-query" in status
            assert "route=sql.execute" in status
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
