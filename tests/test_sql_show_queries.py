from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

show_module = importlib.import_module("analytics_toolkit.sql.metadata.show_queries")
metadata_module = importlib.import_module("analytics_toolkit.sql.metadata")
ch_queries_module = importlib.import_module("analytics_toolkit.sql.backends.ch.queries")
sql_module = importlib.import_module("analytics_toolkit.sql")


def make_config(connection_key: str, backend: str) -> Any:
    return SimpleNamespace(connection_key=connection_key, backend=backend)


def test_show_queries_is_publicly_exported() -> None:
    assert sql_module.show_queries is show_module.show_queries
    assert metadata_module.show_queries is show_module.show_queries
    assert "show_queries" in sql_module.__all__


@pytest.mark.parametrize(
    "raw_state, expected",
    [
        ("active", ["active"]),
        ("finished", ["finished"]),
        ("failed", ["failed"]),
        ("all", ["active", "finished", "failed"]),
        (["active", "failed"], ["active", "failed"]),
        (["active", "all"], ["active", "finished", "failed"]),
    ],
)
def test_normalize_query_states(raw_state: Any, expected: list[str]) -> None:
    assert show_module.normalize_query_states(raw_state) == expected


@pytest.mark.parametrize("raw_state", ["", "queued", [], [object()]])
def test_normalize_query_states_rejects_invalid_values(raw_state: Any) -> None:
    with pytest.raises(ValueError, match="state"):
        show_module.normalize_query_states(raw_state)


def test_clickhouse_query_sql_state_matrix_and_escaped_user() -> None:
    assert ch_queries_module.show_queries_sqls(None, user=None, states=[]) == []
    active = ch_queries_module.show_queries_sqls(None, user="o'reilly", states=["active"])
    assert len(active) == 1
    assert "user = 'o''reilly'" in active[0]["sql"]
    assert "Nullable(DateTime64(6))" in active[0]["sql"]
    assert "Nullable(String)" in active[0]["sql"]
    finished = ch_queries_module.show_queries_sqls(None, user=None, states=["finished"])
    assert "QueryFinish" in finished[0]["sql"]
    assert "ExceptionBeforeStart" not in finished[0]["sql"]
    failed = ch_queries_module.show_queries_sqls(None, user=None, states=["failed"])
    assert "ExceptionBeforeStart" in failed[0]["sql"]
    both = ch_queries_module.show_queries_sqls(
        None, user=None, states=["active", "finished", "failed"]
    )
    assert [item["history"] for item in both] == [False, True]


def test_show_queries_invalid_non_sequence_and_lazy_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="state must be"):
        show_module.normalize_query_states(1)  # type: ignore[arg-type]

    read_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        read_module,
        "read_sql",
        lambda key, query, **kwargs: calls.append((key, query)) or pd.DataFrame(),
    )
    result = show_module._execute_read_sql(
        "ch",
        "select 1",
        print_queries=False,
        retry_cnt=0,
        timeout_increment=0,
        query_label=None,
    )
    assert result.empty
    assert calls == [("ch", "select 1")]


def test_show_queries_active_read_failure_is_not_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        show_module,
        "get_connection_config",
        lambda db_key: make_config(db_key, "ch"),
    )
    monkeypatch.setattr(
        show_module,
        "_read_queries",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("active failed")),
    )
    with pytest.raises(RuntimeError, match="active failed"):
        show_module.show_queries("ch", state="active", retry_cnt=1)


def test_show_queries_normalizer_preserves_existing_backend() -> None:
    result = show_module._normalize_result(
        pd.DataFrame({"backend": ["custom"], "query_id": ["q"]}), "ch"
    )
    assert result.loc[0, "backend"] == "custom"


def test_show_queries_greenplum_active_current_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        show_module,
        "get_connection_config",
        lambda db_key: make_config(db_key, "gp"),
    )

    def fake_read_sql(
        connection_key: str,
        query: str,
        *,
        print_queries: bool,
        retry_cnt: int,
        timeout_increment: float,
        query_label: str | None,
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
        return pd.DataFrame(
            {
                "query_id": [42],
                "user": ["analyst"],
                "state": ["active"],
                "query": ["select 1"],
                "raw_state": ["active"],
            }
        )

    monkeypatch.setattr(show_module, "_execute_read_sql", fake_read_sql)

    result = show_module.show_queries(
        "gp",
        print_queries=True,
        retry_cnt=2,
        timeout_increment=0.5,
        query_label="inspect",
    )

    assert len(calls) == 1
    assert calls[0]["connection_key"] == "gp"
    assert calls[0]["print_queries"] is True
    assert calls[0]["retry_cnt"] == 2
    assert calls[0]["timeout_increment"] == 0.5
    assert calls[0]["query_label"] == "inspect"
    assert "from pg_stat_activity" in calls[0]["query"]
    assert "usename = current_user" in calls[0]["query"]
    assert "state = 'active'" in calls[0]["query"]
    assert result.columns.tolist() == show_module.SHOW_QUERIES_COLUMNS
    assert result["backend"].tolist() == ["gp"]
    assert result["query_id"].tolist() == [42]


def test_show_queries_trino_uses_optional_user_and_history_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []
    monkeypatch.setattr(
        show_module,
        "get_connection_config",
        lambda db_key: make_config(db_key, "trino"),
    )

    def fake_read_sql(connection_key: str, query: str, **kwargs: Any) -> pd.DataFrame:
        del connection_key, kwargs
        queries.append(query)
        return pd.DataFrame(
            {
                "query_id": ["trino-a"],
                "user": ["other'user"],
                "state": ["failed"],
                "query": ["select boom"],
            }
        )

    monkeypatch.setattr(show_module, "_execute_read_sql", fake_read_sql)

    result = show_module.show_queries(
        "trino",
        user="other'user",
        state="failed",
    )

    assert len(queries) == 1
    assert "from system.runtime.queries" in queries[0]
    assert "\"user\" = 'other''user'" in queries[0]
    assert "state in ('FAILED')" in queries[0]
    assert result["backend"].tolist() == ["trino"]
    assert result["state"].tolist() == ["failed"]


def test_show_queries_clickhouse_combines_active_and_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []
    monkeypatch.setattr(
        show_module,
        "get_connection_config",
        lambda db_key: make_config(db_key, "ch"),
    )

    def fake_read_sql(connection_key: str, query: str, **kwargs: Any) -> pd.DataFrame:
        del connection_key, kwargs
        queries.append(query)
        if "from system.processes" in query:
            return pd.DataFrame(
                {
                    "query_id": ["ch-active"],
                    "user": ["default"],
                    "state": ["active"],
                    "query": ["select sleep(10)"],
                }
            )
        return pd.DataFrame(
            {
                "query_id": ["ch-finished"],
                "user": ["default"],
                "state": ["finished"],
                "query": ["select 1"],
            }
        )

    monkeypatch.setattr(show_module, "_execute_read_sql", fake_read_sql)

    result = show_module.show_queries("ch", state="all")

    assert len(queries) == 2
    assert "from system.processes" in queries[0]
    assert "from system.query_log" in queries[1]
    assert (
        "type in ('QueryFinish', 'ExceptionBeforeStart', 'ExceptionWhileProcessing')" in queries[1]
    )
    assert result["query_id"].tolist() == ["ch-active", "ch-finished"]


def test_show_queries_warns_for_greenplum_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        show_module,
        "get_connection_config",
        lambda db_key: make_config(db_key, "gp"),
    )

    with pytest.warns(UserWarning, match="gp does not expose historical query states"):
        result = show_module.show_queries("gp", state="finished")

    assert result.empty
    assert result.columns.tolist() == show_module.SHOW_QUERIES_COLUMNS


def test_show_queries_warns_when_clickhouse_history_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        show_module,
        "get_connection_config",
        lambda db_key: make_config(db_key, "ch"),
    )

    def fake_read_sql(connection_key: str, query: str, **kwargs: Any) -> pd.DataFrame:
        del connection_key, kwargs
        if "from system.query_log" in query:
            raise RuntimeError("query_log unavailable")
        return pd.DataFrame()

    monkeypatch.setattr(show_module, "_execute_read_sql", fake_read_sql)

    with pytest.warns(UserWarning, match="Could not read historical query records"):
        result = show_module.show_queries("ch", state=["active", "finished"])

    assert result.empty
    assert result.columns.tolist() == show_module.SHOW_QUERIES_COLUMNS
