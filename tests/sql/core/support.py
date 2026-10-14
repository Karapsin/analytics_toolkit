"""Regression protection for the shared SQL test drivers and comparisons."""

from __future__ import annotations

import importlib

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.assertions import assert_sql_calls, assert_sql_equal, strip_query_label
from tests.sql._support.scripted import ConnectionSequence, ScriptedConnection, SqlResponse
from tests.sql._support.tasks import patch_task_operations, sql_task_spec


@pytest.mark.parametrize("mode", ["async", "parallel"])
def test_task_operation_seam_isolates_modes_and_restores_original_runners(
    monkeypatch, mode
) -> None:
    module = importlib.import_module(f"analytics_toolkit.sql.orchestration.{mode}_sql")
    original = module.read_sql
    other_mode = "parallel" if mode == "async" else "async"
    other_module = importlib.import_module(f"analytics_toolkit.sql.orchestration.{other_mode}_sql")
    other_original = other_module.read_sql
    rows = pd.DataFrame({"value": [7]})
    calls = []

    def read(**kwargs):
        calls.append(kwargs)
        return rows

    with monkeypatch.context() as patch:
        with pytest.raises(ValueError, match="Unsupported fake task operations"):
            patch_task_operations(patch, mode=mode, read=read, unknown=read)
        assert module.read_sql is original
        patch_task_operations(patch, mode=mode, read=read)
        assert other_module.read_sql is other_original
        result = getattr(sql, f"{mode}_sql")([sql_task_spec("read")], progress=False)
        pd.testing.assert_frame_equal(result["task_0"], rows)
        assert len(calls) == 1
        assert calls[0]["db_key"] == "gp"
        assert calls[0]["query"].endswith("select 1")
    assert module.read_sql is original


@pytest.mark.parametrize("dialect", ["postgres", "trino", "clickhouse"])
def test_sql_comparison_accepts_only_incidental_formatting(dialect: str) -> None:
    assert_sql_equal(
        "select id\nfrom events where id = 1;", "SELECT id FROM events WHERE id=1", dialect=dialect
    )


@pytest.mark.parametrize(
    "actual",
    [
        "SELECT id FROM other WHERE id=1",
        "SELECT ID FROM events WHERE id=1",
        "SELECT id FROM events WHERE id=2",
        "SELECT id FROM events WHERE id<>1",
        "SELECT id FROM events WHERE id=1; SELECT 2",
        "SELECT id FROM events WHERE id=1 -- unexpected comment",
    ],
)
def test_sql_comparison_rejects_contract_changes(actual: str) -> None:
    with pytest.raises(AssertionError, match="SQL differs"):
        assert_sql_equal(actual, "SELECT id FROM events WHERE id=1", dialect="postgres")


def test_sql_comparison_preserves_literal_whitespace_and_comment_labels() -> None:
    with pytest.raises(AssertionError, match="SQL differs"):
        assert_sql_equal("SELECT 'a b'", "SELECT 'a  b'", dialect="postgres")
    with pytest.raises(AssertionError, match="SQL differs"):
        assert_sql_equal(
            "/* label=wrong */ SELECT 1", "/* label=right */ SELECT 1", dialect="postgres"
        )


@pytest.mark.parametrize(
    ("actual", "expected"), [("SELECT (", "SELECT 1"), ("SELECT 1", "SELECT (")]
)
def test_sql_comparison_fails_closed_on_parse_errors(actual: str, expected: str) -> None:
    with pytest.raises(AssertionError, match="Cannot compare invalid"):
        assert_sql_equal(actual, expected, dialect="postgres")


@pytest.mark.parametrize("calls", [["SELECT 2", "SELECT 1"], ["SELECT 1"], ["SELECT 1; SELECT 2"]])
def test_sql_call_comparison_preserves_order_and_batch_boundaries(calls: list[str]) -> None:
    with pytest.raises(AssertionError):
        assert_sql_calls(calls, ["SELECT 1", "SELECT 2"], dialect="postgres")


def test_label_dispatch_removes_only_toolkit_prefix() -> None:
    assert strip_query_label("  /* analytics_toolkit query_label=unit */ SELECT 1") == "SELECT 1"
    assert strip_query_label("/* business rule */ SELECT 1") == "/* business rule */ SELECT 1"
    assert (
        strip_query_label("SELECT '/* analytics_toolkit query_label=unit */'")
        == "SELECT '/* analytics_toolkit query_label=unit */'"
    )


def test_scripted_driver_rejects_wrong_sql_parameters_and_extra_operations() -> None:
    connection = ScriptedConnection([SqlResponse("SELECT ?", params=(7,))], dialect="trino")
    cursor = connection.cursor()
    with pytest.raises(AssertionError, match="SQL differs"):
        cursor.execute("SELECT 8", (7,))
    with pytest.raises(AssertionError, match="parameters"):
        cursor.execute("SELECT ?", (8,))
    cursor.execute("SELECT ?", (7,))
    connection.assert_complete()
    with pytest.raises(AssertionError, match="script exhausted"):
        cursor.execute("SELECT ?", (7,))


def test_read_retries_on_a_fresh_scripted_connection_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")
    first = ScriptedConnection(
        [SqlResponse("SELECT value FROM source_table", error=RuntimeError("temporary"))],
        dialect="postgres",
    )
    second = ScriptedConnection(
        [SqlResponse("SELECT value FROM source_table", rows=((2,),), columns=("value",))],
        dialect="postgres",
    )
    opener = ConnectionSequence([first, second])
    monkeypatch.setattr(module, "get_sql_connection", opener)

    result = module.read_sql(
        "gp", "SELECT value FROM source_table", retry_cnt=2, timeout_increment=0
    )

    pd.testing.assert_frame_equal(result, pd.DataFrame({"value": pd.array([2], dtype="Int64")}))
    assert opener.calls == ["gp", "gp"]
    assert first.rollback_calls == 1
    assert first.close_calls == second.close_calls == 1
    assert all(
        cursor.close_calls == 1 for connection in (first, second) for cursor in connection.cursors
    )
    first.assert_complete()
    second.assert_complete()
    opener.assert_complete()


def test_scripted_factories_report_missing_work_and_isolate_state() -> None:
    response = SqlResponse("SELECT 1", rows=((1,),), columns=("value",))
    first = ScriptedConnection([response], dialect="postgres")
    second = ScriptedConnection([response], dialect="postgres")
    with pytest.raises(AssertionError, match="Unexecuted SQL"):
        first.assert_complete()
    first.cursor().execute("SELECT 1")
    first.assert_complete()
    with pytest.raises(AssertionError, match="Unexecuted SQL"):
        second.assert_complete()
    opener = ConnectionSequence([first])
    with pytest.raises(AssertionError, match="Unused connections"):
        opener.assert_complete()
    assert opener("gp") is first
    with pytest.raises(AssertionError, match="Unexpected connection attempt"):
        opener("gp")


def test_task_factory_returns_independent_mutable_inputs() -> None:
    first = sql_task_spec("load_df")
    first["df"].loc[0, "id"] = 99
    assert sql_task_spec("load_df")["df"].loc[0, "id"] == 1
