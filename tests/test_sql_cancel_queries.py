from __future__ import annotations

import importlib
from typing import Any

import pandas as pd
import pytest


cancel_module = importlib.import_module("analytics_toolkit.sql.dml.io.cancel_queries")
dml_io_module = importlib.import_module("analytics_toolkit.sql.dml.io")
dml_module = importlib.import_module("analytics_toolkit.sql.dml")
sql_module = importlib.import_module("analytics_toolkit.sql")


GP_PID_QUERY = """select pid as query_id
from pg_stat_activity
where usename = current_user
  and pid <> pg_backend_pid()"""
TRINO_QUERY_ID_QUERY = """select query_id
from system.runtime.queries
where "user" = current_user
  and state in ('QUEUED', 'RUNNING')
  and query not like '%system.runtime.queries%'"""
CH_QUERY_ID_QUERY = """select query_id
from system.processes
where user = currentUser()
  and query_id != currentQueryID()"""


def test_cancel_queries_is_exported_and_gp_helper_is_removed() -> None:
    assert sql_module.cancel_queries is cancel_module.cancel_queries
    assert dml_module.cancel_queries is cancel_module.cancel_queries
    assert dml_io_module.cancel_queries is cancel_module.cancel_queries
    assert "gp_cancel_all_running_queries" not in sql_module.__all__
    assert not hasattr(sql_module, "gp_cancel_all_running_queries")


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({}, "Provide query_ids"),
        ({"query_ids": "abc", "cancel_all": True}, "not both"),
        ({"query_ids": []}, "must not be empty"),
        ({"query_ids": [True]}, "strings or integers"),
        ({"query_ids": object()}, "string, integer, or sequence"),
    ],
)
def test_cancel_queries_rejects_invalid_query_id_modes(
    kwargs: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        cancel_module.cancel_queries("gp", **kwargs)


@pytest.mark.parametrize("concurrency", [0, -1, True, 1.5])
def test_cancel_queries_rejects_invalid_concurrency(concurrency: Any) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        cancel_module.cancel_queries("gp", [42], concurrency=concurrency)


def test_cancel_queries_gp_explicit_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_read_sql(
        connection_key: str,
        query: str,
        print_queries: bool = True,
        retry_cnt: int = 5,
        timeout_increment: int | float = 5,
        query_label: str | None = None,
    ) -> pd.DataFrame:
        calls.append(
            {
                "connection_key": connection_key,
                "query": query,
                "print_queries": print_queries,
                "retry_cnt": retry_cnt,
                "timeout_increment": timeout_increment,
                "query_label": query_label,
            }
        )
        if query == "select pg_cancel_backend(42) as cancelled":
            return pd.DataFrame({"cancelled": [True]})
        if query == "select pg_cancel_backend(7) as cancelled":
            return pd.DataFrame({"cancelled": [False]})
        raise AssertionError(f"Unexpected query: {query}")

    monkeypatch.setattr(cancel_module, "read_sql", fake_read_sql)

    result = cancel_module.cancel_queries(
        "gp",
        [42, "7"],
        print_queries=True,
        retry_cnt=2,
        timeout_increment=0.5,
        query_label="cancel-tests",
    )

    assert [call["query"] for call in calls] == [
        "select pg_cancel_backend(42) as cancelled",
        "select pg_cancel_backend(7) as cancelled",
    ]
    assert all(call["connection_key"] == "gp" for call in calls)
    assert all(call["print_queries"] is True for call in calls)
    assert all(call["retry_cnt"] == 2 for call in calls)
    assert all(call["timeout_increment"] == 0.5 for call in calls)
    assert all(call["query_label"] == "cancel-tests" for call in calls)
    pd.testing.assert_frame_equal(
        result,
        pd.DataFrame(
            {
                "backend": ["gp", "gp"],
                "query_id": [42, 7],
                "cancel_query": [
                    "select pg_cancel_backend(42) as cancelled",
                    "select pg_cancel_backend(7) as cancelled",
                ],
                "cancelled": [True, False],
                "status": ["cancelled", "not_cancelled"],
            }
        ),
    )


def test_cancel_queries_gp_cancel_all_discovers_current_user_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_read_sql(
        connection_key: str,
        query: str,
        print_queries: bool = True,
        retry_cnt: int = 5,
        timeout_increment: int | float = 5,
        query_label: str | None = None,
    ) -> pd.DataFrame:
        del connection_key, print_queries, retry_cnt, timeout_increment, query_label
        calls.append(query)
        if query == GP_PID_QUERY:
            return pd.DataFrame({"query_id": [3, 1, 2]})
        if query.startswith("select pg_cancel_backend("):
            return pd.DataFrame({"cancelled": [query.endswith("(1) as cancelled")]})
        raise AssertionError(f"Unexpected query: {query}")

    monkeypatch.setattr(cancel_module, "read_sql", fake_read_sql)

    result = cancel_module.cancel_queries("gp", cancel_all=True)

    assert calls == [
        GP_PID_QUERY,
        "select pg_cancel_backend(3) as cancelled",
        "select pg_cancel_backend(1) as cancelled",
        "select pg_cancel_backend(2) as cancelled",
    ]
    assert result["query_id"].tolist() == [3, 1, 2]
    assert result["cancelled"].tolist() == [False, True, False]


def test_cancel_queries_concurrent_path_preserves_result_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingExecutor:
        max_workers_seen: list[int] = []
        mapped_items: list[list[int | str]] = []

        def __init__(self, max_workers: int) -> None:
            self.max_workers = max_workers
            self.max_workers_seen.append(max_workers)

        def __enter__(self) -> RecordingExecutor:
            return self

        def __exit__(
            self,
            exc_type: object,
            exc: object,
            traceback: object,
        ) -> None:
            return None

        def map(self, fn: Any, iterable: Any) -> list[dict[str, object]]:
            items = list(iterable)
            self.mapped_items.append(items)
            return [fn(item) for item in items]

    def fake_read_sql(
        connection_key: str,
        query: str,
        print_queries: bool = True,
        retry_cnt: int = 5,
        timeout_increment: int | float = 5,
        query_label: str | None = None,
    ) -> pd.DataFrame:
        del connection_key, print_queries, retry_cnt, timeout_increment, query_label
        if query.startswith("select pg_cancel_backend("):
            return pd.DataFrame({"cancelled": [True]})
        raise AssertionError(f"Unexpected query: {query}")

    monkeypatch.setattr(cancel_module, "ThreadPoolExecutor", RecordingExecutor)
    monkeypatch.setattr(cancel_module, "read_sql", fake_read_sql)

    result = cancel_module.cancel_queries("gp", [3, 1, 2], concurrency=3)

    assert RecordingExecutor.max_workers_seen == [3]
    assert RecordingExecutor.mapped_items == [[3, 1, 2]]
    assert result["query_id"].tolist() == [3, 1, 2]


def test_cancel_queries_trino_explicit_ids_uses_kill_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_read_sql(
        connection_key: str,
        query: str,
        print_queries: bool = True,
        retry_cnt: int = 5,
        timeout_increment: int | float = 5,
        query_label: str | None = None,
    ) -> pd.DataFrame:
        del connection_key, print_queries, retry_cnt, timeout_increment, query_label
        calls.append(query)
        return pd.DataFrame()

    monkeypatch.setattr(cancel_module, "read_sql", fake_read_sql)

    result = cancel_module.cancel_queries("trino", ["20240610_1", "id'2"])

    assert calls == [
        "CALL system.runtime.kill_query("
        "query_id => '20240610_1', "
        "message => 'Cancelled by analytics_toolkit.cancel_queries')",
        "CALL system.runtime.kill_query("
        "query_id => 'id''2', "
        "message => 'Cancelled by analytics_toolkit.cancel_queries')",
    ]
    assert result["backend"].tolist() == ["trino", "trino"]
    assert result["query_id"].tolist() == ["20240610_1", "id'2"]
    assert result["cancelled"].tolist() == [True, True]
    assert result["status"].tolist() == ["submitted", "submitted"]


def test_cancel_queries_trino_cancel_all_discovers_current_user_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_read_sql(
        connection_key: str,
        query: str,
        print_queries: bool = True,
        retry_cnt: int = 5,
        timeout_increment: int | float = 5,
        query_label: str | None = None,
    ) -> pd.DataFrame:
        del connection_key, print_queries, retry_cnt, timeout_increment, query_label
        calls.append(query)
        if query == TRINO_QUERY_ID_QUERY:
            return pd.DataFrame({"query_id": ["trino-a", "trino-b"]})
        return pd.DataFrame()

    monkeypatch.setattr(cancel_module, "read_sql", fake_read_sql)

    result = cancel_module.cancel_queries("trino", cancel_all=True)

    assert calls == [
        TRINO_QUERY_ID_QUERY,
        "CALL system.runtime.kill_query("
        "query_id => 'trino-a', "
        "message => 'Cancelled by analytics_toolkit.cancel_queries')",
        "CALL system.runtime.kill_query("
        "query_id => 'trino-b', "
        "message => 'Cancelled by analytics_toolkit.cancel_queries')",
    ]
    assert result["query_id"].tolist() == ["trino-a", "trino-b"]


def test_cancel_queries_clickhouse_explicit_ids_uses_kill_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_read_sql(
        connection_key: str,
        query: str,
        print_queries: bool = True,
        retry_cnt: int = 5,
        timeout_increment: int | float = 5,
        query_label: str | None = None,
    ) -> pd.DataFrame:
        del connection_key, print_queries, retry_cnt, timeout_increment, query_label
        calls.append(query)
        return pd.DataFrame({"kill_status": ["finished"]})

    monkeypatch.setattr(cancel_module, "read_sql", fake_read_sql)

    result = cancel_module.cancel_queries("ch", ["ch-a"])

    assert calls == ["KILL QUERY WHERE query_id = 'ch-a' SYNC"]
    assert result["backend"].tolist() == ["ch"]
    assert result["query_id"].tolist() == ["ch-a"]
    assert result["cancelled"].tolist() == [True]
    assert result["status"].tolist() == ["finished"]


def test_cancel_queries_clickhouse_cancel_all_discovers_current_user_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_read_sql(
        connection_key: str,
        query: str,
        print_queries: bool = True,
        retry_cnt: int = 5,
        timeout_increment: int | float = 5,
        query_label: str | None = None,
    ) -> pd.DataFrame:
        del connection_key, print_queries, retry_cnt, timeout_increment, query_label
        calls.append(query)
        if query == CH_QUERY_ID_QUERY:
            return pd.DataFrame({"query_id": ["ch-a", "ch-b"]})
        return pd.DataFrame({"kill_status": ["waiting"]})

    monkeypatch.setattr(cancel_module, "read_sql", fake_read_sql)

    result = cancel_module.cancel_queries("ch", cancel_all=True)

    assert calls == [
        CH_QUERY_ID_QUERY,
        "KILL QUERY WHERE query_id = 'ch-a' SYNC",
        "KILL QUERY WHERE query_id = 'ch-b' SYNC",
    ]
    assert result["query_id"].tolist() == ["ch-a", "ch-b"]
    assert result["cancelled"].tolist() == [False, False]
    assert result["status"].tolist() == ["waiting", "waiting"]


def test_cancel_queries_cancel_all_empty_result_returns_expected_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cancel_module,
        "read_sql",
        lambda *args, **kwargs: pd.DataFrame({"query_id": []}),
    )

    result = cancel_module.cancel_queries("trino", cancel_all=True)

    assert result.empty
    assert result.columns.tolist() == [
        "backend",
        "query_id",
        "cancel_query",
        "cancelled",
        "status",
    ]
