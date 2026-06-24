from __future__ import annotations

import importlib
from typing import Any

import pandas as pd
import pytest


cancel_module = importlib.import_module("analytics_toolkit.sql.dml.io.cancel_queries")
dml_io_module = importlib.import_module("analytics_toolkit.sql.dml.io")
dml_module = importlib.import_module("analytics_toolkit.sql.dml")
sql_module = importlib.import_module("analytics_toolkit.sql")


def gp_cancel_terminate_sql(pid: int) -> str:
    return f"""with cancel_attempt as (
    select pg_cancel_backend({pid}) as cancelled
)
select cancelled, pg_terminate_backend({pid}) as terminated
from cancel_attempt"""


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
        if query == gp_cancel_terminate_sql(42):
            return pd.DataFrame({"cancelled": [True], "terminated": [True]})
        if query == gp_cancel_terminate_sql(7):
            return pd.DataFrame({"cancelled": [False], "terminated": [True]})
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
        gp_cancel_terminate_sql(42),
        gp_cancel_terminate_sql(7),
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
                    gp_cancel_terminate_sql(42),
                    gp_cancel_terminate_sql(7),
                ],
                "cancelled": [True, False],
                "terminated": [True, True],
                "status": ["cancelled_terminated", "not_cancelled_terminated"],
            }
        ),
    )


def test_cancel_queries_gp_cancel_all_discovers_current_user_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    show_calls: list[dict[str, object]] = []

    def fake_show_queries(
        db_key: str,
        *,
        state: str,
        print_queries: bool,
        retry_cnt: int,
        timeout_increment: int | float,
        query_label: str | None,
    ) -> pd.DataFrame:
        show_calls.append(
            {
                "db_key": db_key,
                "state": state,
                "print_queries": print_queries,
                "retry_cnt": retry_cnt,
                "timeout_increment": timeout_increment,
                "query_label": query_label,
            }
        )
        return pd.DataFrame({"query_id": [3, 1, 2]})

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
        if query in {
            gp_cancel_terminate_sql(1),
            gp_cancel_terminate_sql(2),
            gp_cancel_terminate_sql(3),
        }:
            return pd.DataFrame(
                {
                    "cancelled": [query == gp_cancel_terminate_sql(1)],
                    "terminated": [query != gp_cancel_terminate_sql(2)],
                }
            )
        raise AssertionError(f"Unexpected query: {query}")

    monkeypatch.setattr(cancel_module, "show_queries", fake_show_queries)
    monkeypatch.setattr(cancel_module, "read_sql", fake_read_sql)

    result = cancel_module.cancel_queries(
        "gp",
        cancel_all=True,
        print_queries=True,
        retry_cnt=2,
        timeout_increment=0.5,
        query_label="cancel-all",
    )

    assert show_calls == [
        {
            "db_key": "gp",
            "state": "active",
            "print_queries": True,
            "retry_cnt": 2,
            "timeout_increment": 0.5,
            "query_label": "cancel-all",
        }
    ]
    assert calls == [
        gp_cancel_terminate_sql(3),
        gp_cancel_terminate_sql(1),
        gp_cancel_terminate_sql(2),
    ]
    assert result["query_id"].tolist() == [3, 1, 2]
    assert result["cancelled"].tolist() == [False, True, False]
    assert result["terminated"].tolist() == [True, True, False]
    assert result["status"].tolist() == [
        "not_cancelled_terminated",
        "cancelled_terminated",
        "not_cancelled_not_terminated",
    ]


def test_cancel_queries_gp_reports_cancelled_not_terminated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cancel_module,
        "read_sql",
        lambda *args, **kwargs: pd.DataFrame(
            {"cancelled": [True], "terminated": [False]}
        ),
    )

    result = cancel_module.cancel_queries("gp", [8])

    assert result["query_id"].tolist() == [8]
    assert result["cancelled"].tolist() == [True]
    assert result["terminated"].tolist() == [False]
    assert result["status"].tolist() == ["cancelled_not_terminated"]


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
        if query in {
            gp_cancel_terminate_sql(1),
            gp_cancel_terminate_sql(2),
            gp_cancel_terminate_sql(3),
        }:
            return pd.DataFrame({"cancelled": [True], "terminated": [True]})
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
    assert result["terminated"].tolist() == [None, None]
    assert result["status"].tolist() == ["submitted", "submitted"]


def test_cancel_queries_trino_cancel_all_discovers_current_user_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        cancel_module,
        "show_queries",
        lambda *args, **kwargs: pd.DataFrame({"query_id": ["trino-a", "trino-b"]}),
    )

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

    result = cancel_module.cancel_queries("trino", cancel_all=True)

    assert calls == [
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
    assert result["terminated"].tolist() == [None]
    assert result["status"].tolist() == ["finished"]


def test_cancel_queries_clickhouse_cancel_all_discovers_current_user_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        cancel_module,
        "show_queries",
        lambda *args, **kwargs: pd.DataFrame({"query_id": ["ch-a", "ch-b"]}),
    )

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
        return pd.DataFrame({"kill_status": ["waiting"]})

    monkeypatch.setattr(cancel_module, "read_sql", fake_read_sql)

    result = cancel_module.cancel_queries("ch", cancel_all=True)

    assert calls == [
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
        "show_queries",
        lambda *args, **kwargs: pd.DataFrame({"query_id": []}),
    )

    result = cancel_module.cancel_queries("trino", cancel_all=True)

    assert result.empty
    assert result.columns.tolist() == [
        "backend",
        "query_id",
        "cancel_query",
        "cancelled",
        "terminated",
        "status",
    ]
