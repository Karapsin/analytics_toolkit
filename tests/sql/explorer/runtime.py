from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest
from analytics_toolkit.sql_explorer import runtime
from analytics_toolkit.sql_explorer.errors import SqlExplorerConfigurationError
from analytics_toolkit.sql_explorer.settings import ExplorerSettings
from analytics_toolkit.sql_explorer.statements import ExecutionRoute, build_execution_plan

if TYPE_CHECKING:
    from pathlib import Path


def _valid_connection(key: str = "gp") -> SimpleNamespace:
    return SimpleNamespace(connection_key=key, backend="gp", valid=True, error=None)


def _session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> runtime.ExplorerSession:
    monkeypatch.setattr(
        runtime.sql,
        "validate_connections",
        lambda keys, connect: [_valid_connection(keys[0])],
    )
    return runtime.ExplorerSession("gp", settings_path=tmp_path / "settings.json")


def test_session_validates_database_and_persists_preferences(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session(monkeypatch, tmp_path)

    assert session.database.connection_key == "gp"
    assert session.set_run_binding("F8").run_binding == "f8"
    assert session.set_run_binding("reset").run_binding == "ctrl+enter"
    assert session.set_confirmation(enabled=False).confirm_mutations is False
    assert runtime.load_settings(tmp_path / "settings.json").settings == ExplorerSettings(
        confirm_mutations=False
    )


def test_session_switches_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = _session(monkeypatch, tmp_path)

    selected = session.switch_database("warehouse")

    assert selected.connection_key == "warehouse"
    assert session.database == selected
    assert session.plan("select 1").route is ExecutionRoute.READ


def test_read_execution_displays_only_two_hundred_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session(monkeypatch, tmp_path)
    calls: list[tuple[str, str, dict[str, Any]]] = []
    frame = pd.DataFrame({"value": range(201)})

    def fake_read(db_key: str, query: str, **kwargs: Any) -> SimpleNamespace:
        calls.append((db_key, query, kwargs))
        return SimpleNamespace(data=frame)

    monkeypatch.setattr(runtime.sql, "read", fake_read)
    result = session.execute(build_execution_plan("select value from sample", "gp"))

    assert result.route is ExecutionRoute.READ
    assert result.displayed_rows == 200
    assert result.total_rows is None
    assert result.truncated is True
    assert result.dataframe is not None
    assert len(result.dataframe) == 200
    assert result.status == "Showing the first 200 rows; more rows are available."
    assert calls[0][0] == "gp"
    assert "LIMIT 201" in calls[0][1]
    assert calls[0][2]["retry_cnt"] == 1
    assert calls[0][2]["return_metadata"] is True
    assert calls[0][2]["query_label"].startswith("sql_explorer run=")
    assert session.active_query_label is None


def test_unwrapped_result_reports_known_total(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session(monkeypatch, tmp_path)
    frame = pd.DataFrame({"value": range(205)})
    monkeypatch.setattr(runtime.sql, "read", lambda *args, **kwargs: frame)

    result = session.execute(build_execution_plan("show tables", "gp"))

    assert result.total_rows == 205
    assert result.status == "Showing the first 200 of 205 rows."


def test_export_replays_capped_query_without_display_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session(monkeypatch, tmp_path)
    calls: list[str] = []

    def fake_read(_db_key: str, query: str, **_kwargs: Any) -> pd.DataFrame:
        calls.append(query)
        return pd.DataFrame({"value": range(201 if len(calls) == 1 else 205)})

    monkeypatch.setattr(runtime.sql, "read", fake_read)
    session.execute(build_execution_plan("select value from sample", "gp"))

    state = session.export_state()
    assert state.truncated is True
    assert state.dataframe is None
    assert len(session.export_dataframe()) == 205
    assert "LIMIT 201" in calls[0]
    assert "LIMIT 201" not in calls[1]
    assert calls[1] == "select value from sample"


def test_export_uses_cached_unwrapped_result_without_replaying(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session(monkeypatch, tmp_path)
    calls: list[str] = []

    def fake_read(_db_key: str, query: str, **_kwargs: Any) -> pd.DataFrame:
        calls.append(query)
        return pd.DataFrame({"value": range(205)})

    monkeypatch.setattr(runtime.sql, "read", fake_read)
    session.execute(build_execution_plan("show tables", "gp"))

    assert len(session.export_dataframe()) == 205
    assert calls == ["show tables"]


def test_export_requires_results_and_replays_execute_read_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session(monkeypatch, tmp_path)
    plan = build_execution_plan("create temp table x(a int); select * from x", "gp")
    session._export_state = runtime.ExplorerExportState(plan, None, truncated=True)
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_execute_read(_db_key: str, query: str, **kwargs: Any) -> SimpleNamespace:
        calls.append((query, kwargs))
        session.active_query_label = "another query"
        session.active_query = None
        return SimpleNamespace(data=pd.DataFrame({"value": [1, 2]}))

    monkeypatch.setattr(runtime.sql, "execute_read", fake_execute_read)

    assert session.export_dataframe().to_dict(orient="list") == {"value": [1, 2]}
    assert calls[0][0] == plan.full_execution_sql
    assert calls[0][1]["query_label"].startswith("sql_explorer export=")
    assert session.active_query_label == "another query"

    session._export_state = None
    with pytest.raises(SqlExplorerConfigurationError, match="row-producing"):
        session.export_state()


def test_failed_export_marks_query_failed_and_clears_active_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session(monkeypatch, tmp_path)
    plan = build_execution_plan("select value from sample", "gp")
    session._export_state = runtime.ExplorerExportState(plan, None, truncated=True)

    def fail_read(*_args: Any, **_kwargs: Any) -> pd.DataFrame:
        raise RuntimeError

    monkeypatch.setattr(runtime.sql, "read", fail_read)

    with pytest.raises(RuntimeError):
        session.export_dataframe()

    assert session.active_query_label is None
    assert session.active_query is None
    assert session.last_query is not None
    assert session.last_query.state == "failed"


def test_execute_read_and_execute_are_dispatched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session(monkeypatch, tmp_path)
    execute_read_calls: list[dict[str, Any]] = []
    execute_calls: list[dict[str, Any]] = []

    def fake_execute_read(*args: Any, **kwargs: Any) -> SimpleNamespace:
        execute_read_calls.append(kwargs)
        return SimpleNamespace(data=pd.DataFrame({"value": [1]}))

    def fake_execute(*args: Any, **kwargs: Any) -> SimpleNamespace:
        execute_calls.append(kwargs)
        return SimpleNamespace(data=None)

    monkeypatch.setattr(runtime.sql, "execute_read", fake_execute_read)
    monkeypatch.setattr(runtime.sql, "execute", fake_execute)

    read_result = session.execute(
        build_execution_plan("create temp table x(a int); select * from x", "gp")
    )
    execute_result = session.execute(build_execution_plan("delete from x", "gp"))

    assert read_result.route is ExecutionRoute.EXECUTE_READ
    assert read_result.status == "Returned 1 row(s)."
    assert execute_result.route is ExecutionRoute.EXECUTE
    assert execute_result.dataframe is None
    assert execute_result.status == "Executed 1 statement(s) successfully."
    assert execute_read_calls[0]["return_metadata"] is True
    assert execute_calls[0]["retry_policy"] == "safe"


def test_row_query_requires_dataframe_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime.sql, "read", lambda *args, **kwargs: SimpleNamespace(data=1))

    with pytest.raises(TypeError, match="dataframe"):
        session.execute(build_execution_plan("select 1", "gp"))


def test_cancel_active_targets_only_matching_explorer_query(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session(monkeypatch, tmp_path)
    session.active_query_label = "sql_explorer run=target"
    running = pd.DataFrame(
        {
            "query_id": [10, 20],
            "query": [
                "/* analytics_toolkit query_label=sql_explorer run=target */ select 1",
                "/* analytics_toolkit query_label=someone_else */ select 2",
            ],
        }
    )
    calls: list[tuple[list[int], dict[str, Any]]] = []
    monkeypatch.setattr(runtime.sql, "show_queries", lambda *args, **kwargs: running)

    def fake_cancel(db_key: str, query_ids: list[int], **kwargs: Any) -> pd.DataFrame:
        assert db_key == "gp"
        calls.append((query_ids, kwargs))
        return pd.DataFrame({"cancelled": [True], "terminated": [False]})

    monkeypatch.setattr(runtime.sql, "cancel_queries", fake_cancel)

    result = session.cancel_active()

    assert result.matched_queries == 1
    assert result.cancelled_queries == 1
    assert calls[0][0] == [10]
    assert calls[0][1]["retry_cnt"] == 1
    assert result.status == "Cancellation requested for 1 of 1 matching query(s)."


def test_cancel_active_without_run_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session(monkeypatch, tmp_path)

    result = session.cancel_active()

    assert result.matched_queries == 0
    assert result.cancelled_queries == 0
    assert result.status == "No active explorer query was found."


def test_cancel_active_handles_query_that_is_not_visible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session(monkeypatch, tmp_path)
    session.active_query_label = "sql_explorer run=missing"
    calls: list[int] = []
    monkeypatch.setattr(
        runtime.sql,
        "show_queries",
        lambda *args, **kwargs: calls.append(1) or pd.DataFrame({"other": []}),
    )
    monkeypatch.setattr(runtime, "sleep", lambda delay: None)

    result = session.cancel_active()

    assert len(calls) == 5
    assert result.matched_queries == 0
    assert "nothing was cancelled" in result.status


def test_execute_preserves_a_newer_active_label(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session(monkeypatch, tmp_path)

    def fake_read(*args: Any, **kwargs: Any) -> pd.DataFrame:
        session.active_query_label = "newer run"
        return pd.DataFrame({"value": [1]})

    monkeypatch.setattr(runtime.sql, "read", fake_read)

    session.execute(build_execution_plan("select 1", "gp"))

    assert session.active_query_label == "newer run"


@pytest.mark.parametrize(
    ("results", "message"),
    [
        ([], "was not found"),
        ([_valid_connection()], "invalid"),
    ],
)
def test_invalid_database_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    results: list[SimpleNamespace],
    message: str,
) -> None:
    if results:
        results[0].valid = False
        results[0].error = "bad config"
    monkeypatch.setattr(runtime.sql, "validate_connections", lambda keys, connect: results)

    with pytest.raises(SqlExplorerConfigurationError, match=message):
        runtime.validate_database("missing")
