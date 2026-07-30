from __future__ import annotations

import importlib
import threading
import time
from typing import Any

import pytest

execute_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_sql")


def test_execute_sql_list_returns_results_in_input_order(monkeypatch) -> None:
    def fake_execute(options: Any) -> str:
        if options.sql == "select 1":
            time.sleep(0.03)
        return options.sql

    monkeypatch.setattr(execute_sql_module, "_execute_sql_options", fake_execute)

    result = execute_sql_module.execute_sql(
        "gp",
        ["select 1", "select 2", "select 3"],
        concurrency=3,
        hard_concurrency_cap=3,
    )

    assert result == ["select 1", "select 2", "select 3"]


def test_execute_sql_list_defaults_to_sequential_execution(monkeypatch) -> None:
    calls: list[str] = []

    def fake_execute(options: Any) -> None:
        calls.append(options.sql)

    monkeypatch.setattr(execute_sql_module, "_execute_sql_options", fake_execute)

    result = execute_sql_module.execute_sql(
        "gp",
        ["select 1", "select 2"],
    )

    assert result == [None, None]
    assert calls == ["select 1", "select 2"]


def test_execute_sql_soft_cap_limits_active_workers(monkeypatch) -> None:
    lock = threading.Lock()
    two_workers_started = threading.Event()
    active_workers = 0
    max_active_workers = 0

    def fake_execute(options: Any) -> str:
        nonlocal active_workers, max_active_workers
        with lock:
            active_workers += 1
            max_active_workers = max(max_active_workers, active_workers)
            if active_workers == 2:
                two_workers_started.set()
        assert two_workers_started.wait(timeout=1)
        time.sleep(0.02)
        with lock:
            active_workers -= 1
        return options.sql

    monkeypatch.setattr(execute_sql_module, "_execute_sql_options", fake_execute)

    result = execute_sql_module.execute_sql(
        "gp",
        ["select 1", "select 2", "select 3"],
        concurrency=3,
        soft_concurrency_cap=2,
        hard_concurrency_cap=2,
    )

    assert result == ["select 1", "select 2", "select 3"]
    assert max_active_workers == 2


def test_execute_sql_hard_cap_rejects_effective_batch_concurrency() -> None:
    with pytest.raises(ValueError, match="effective concurrency exceeds"):
        execute_sql_module.execute_sql(
            "gp",
            ["select 1", "select 2"],
            concurrency=3,
            hard_concurrency_cap=2,
            dry_run=True,
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"concurrency": 0}, "concurrency"),
        ({"concurrency": True}, "concurrency"),
        ({"soft_concurrency_cap": 0}, "soft_concurrency_cap"),
        ({"soft_concurrency_cap": True}, "soft_concurrency_cap"),
        ({"hard_concurrency_cap": 0}, "hard_concurrency_cap"),
        ({"hard_concurrency_cap": True}, "hard_concurrency_cap"),
    ],
)
def test_execute_sql_rejects_invalid_concurrency_options(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        execute_sql_module.execute_sql("gp", ["select 1"], **kwargs)


@pytest.mark.parametrize(
    ("query", "error", "message"),
    [
        ([], execute_sql_module.InvalidSqlInputError, "must not be empty"),
        (["select 1", " "], execute_sql_module.InvalidSqlInputError, "index 1"),
        (["select 1", 2], TypeError, "index 1"),
        (("select 1", "select 2"), TypeError, "string or a non-empty list"),
    ],
)
def test_execute_sql_validates_batch_before_connection_lookup(
    monkeypatch,
    query: Any,
    error: type[Exception],
    message: str,
) -> None:
    monkeypatch.setattr(
        execute_sql_module,
        "get_connection_config",
        lambda _key: pytest.fail("connection config should not be read"),
    )

    with pytest.raises(error, match=message):
        execute_sql_module.execute_sql("gp", query)


def test_execute_sql_list_dry_run_returns_ordered_plans() -> None:
    plans = execute_sql_module.execute_sql(
        "trino",
        ["select 1", "select 2"],
        concurrency=2,
        hard_concurrency_cap=2,
        dry_run=True,
    )

    assert [plan.sqls for plan in plans] == [["select 1"], ["select 2"]]


def test_execute_sql_retries_failed_item_then_fails_fast(monkeypatch) -> None:
    class FakeConnection:
        def __init__(self, name: str) -> None:
            self.name = name
            self.rollback_calls = 0
            self.close_calls = 0

        def rollback(self) -> None:
            self.rollback_calls += 1

        def close(self) -> None:
            self.close_calls += 1

    connections = [FakeConnection("first"), FakeConnection("second")]
    executed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        execute_sql_module,
        "get_sql_connection",
        lambda _key: connections[len(executed)],
    )

    def fake_execute(
        connection: FakeConnection,
        query: str,
        **_kwargs: Any,
    ) -> None:
        executed.append((connection.name, query))
        message = "temporary failure"
        raise RuntimeError(message)

    adapter = execute_sql_module.get_backend_adapter("trino")
    monkeypatch.setattr(adapter, "execute_sql", fake_execute)

    with pytest.raises(RuntimeError, match="temporary failure"):
        execute_sql_module.execute_sql(
            "trino",
            ["select fail", "select must_not_run"],
            concurrency=1,
            retry_cnt=2,
            timeout_increment=0,
        )

    assert executed == [
        ("first", "select fail"),
        ("second", "select fail"),
    ]
    assert [connection.rollback_calls for connection in connections] == [0, 0]
    assert [connection.close_calls for connection in connections] == [1, 1]


def test_execute_sql_concurrent_failure_unregisters_cancellation_scope(
    monkeypatch,
) -> None:
    class FakeCancellationScope:
        def __init__(self) -> None:
            self.registered: list[Any] = []
            self.unregistered: list[Any] = []

        def register_executor(self, executor: Any) -> None:
            self.registered.append(executor)

        def unregister_executor(self, executor: Any) -> None:
            self.unregistered.append(executor)

    scope = FakeCancellationScope()

    def fake_execute(options: Any) -> None:
        if options.sql == "select fail":
            message = "batch failure"
            raise RuntimeError(message)

    monkeypatch.setattr(execute_sql_module, "current_cancellation_scope", lambda: scope)
    monkeypatch.setattr(execute_sql_module, "_execute_sql_options", fake_execute)

    with pytest.raises(RuntimeError, match="batch failure"):
        execute_sql_module.execute_sql(
            "gp",
            ["select fail", "select 2", "select 3"],
            concurrency=2,
        )

    assert len(scope.registered) == 1
    assert scope.unregistered == scope.registered
