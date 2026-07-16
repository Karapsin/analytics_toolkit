from __future__ import annotations

import asyncio
import importlib
import threading
import time
from typing import Any

import pandas as pd
import pytest

async_module = importlib.import_module("analytics_toolkit.sql.orchestration.async_sql")
cancellation_module = importlib.import_module("analytics_toolkit.sql.execution.cancellation")
cancel_queries_module = importlib.import_module("analytics_toolkit.sql.dml.io.cancel_queries")
connection_module = importlib.import_module("analytics_toolkit.sql.connection.get_sql_connection")
labels_module = importlib.import_module("analytics_toolkit.sql.execution.labels")
retry_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.runtime.retry")
show_queries_module = importlib.import_module("analytics_toolkit.sql.metadata.show_queries")


def test_async_sql_cancellation_marker_preserves_user_query_label() -> None:
    scope = cancellation_module.SqlCancellationScope()

    with cancellation_module.activate_cancellation_scope(scope):
        labelled = labels_module.apply_query_label("SELECT 1", "daily report")
        repeated = labels_module.apply_query_label(labelled, "daily report")

    marker_comment = f"/* analytics_toolkit {scope.marker} */"
    assert labelled.splitlines() == [
        marker_comment,
        "/* analytics_toolkit query_label=daily report */",
        "SELECT 1",
    ]
    assert repeated == labelled


def test_nested_async_sql_markers_include_parent_marker() -> None:
    parent = cancellation_module.SqlCancellationScope()
    child = cancellation_module.SqlCancellationScope(parent=parent)

    with cancellation_module.activate_cancellation_scope(child):
        labelled = labels_module.apply_query_label("SELECT 1", None)

    assert labelled.splitlines() == [
        f"/* analytics_toolkit {parent.marker} */",
        f"/* analytics_toolkit {child.marker} */",
        "SELECT 1",
    ]

    with cancellation_module.activate_cancellation_scope(child):
        cancellation_module.register_connection_alias("gp")
    assert child.aliases == ("gp",)
    assert parent.aliases == ("gp",)


def test_cancelled_scope_rejects_late_executor_registration() -> None:
    scope = cancellation_module.SqlCancellationScope()
    executor = FakeExecutor()
    scope.request_cancel()

    scope.register_executor(executor)  # type: ignore[arg-type]
    scope.unregister_executor(executor)  # type: ignore[arg-type]

    assert executor.calls == [(False, True)]


def test_wait_helper_supports_plain_and_cancellable_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(cancellation_module.time, "sleep", sleeps.append)
    cancellation_module.wait_or_raise_if_cancelled(0.25)
    assert sleeps == [0.25]

    scope = cancellation_module.SqlCancellationScope()
    with cancellation_module.activate_cancellation_scope(scope):
        cancellation_module.wait_or_raise_if_cancelled(0.001)
        scope.request_cancel()
        with pytest.raises(cancellation_module.AsyncSqlCancelled):
            cancellation_module.wait_or_raise_if_cancelled(1)


def test_cancel_scope_queries_without_aliases_is_a_noop() -> None:
    cancellation_module.cancel_scope_queries(
        cancellation_module.SqlCancellationScope(),
        timeout=0,
    )


def test_matching_query_ids_selects_only_the_exact_batch_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = pd.DataFrame(
        {
            "query_id": [1, 2, 3],
            "query": [
                "/* analytics_toolkit async_sql=target */ SELECT 1",
                "/* analytics_toolkit async_sql=other */ SELECT 2",
                "SELECT 'async_sql=targetish'",
            ],
        }
    )
    monkeypatch.setattr(show_queries_module, "show_queries", lambda *args, **kwargs: rows)

    assert cancellation_module._matching_query_ids("gp", "async_sql=target") == [1]


def test_cancel_scope_queries_cancels_registered_aliases_until_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = cancellation_module.SqlCancellationScope()
    scope.register_alias("gp")
    scope.register_alias("trino")
    calls: dict[str, int] = {"gp": 0, "trino": 0}
    cancelled: list[tuple[str, tuple[int | str, ...]]] = []
    lock = threading.Lock()

    def matching(connection_key: str, marker: str) -> list[int | str]:
        assert marker == scope.marker
        with lock:
            calls[connection_key] += 1
            return [f"{connection_key}-query"] if calls[connection_key] == 1 else []

    def cancel(connection_key: str, query_ids: list[int | str]) -> None:
        with lock:
            cancelled.append((connection_key, tuple(query_ids)))

    monkeypatch.setattr(cancellation_module, "_matching_query_ids", matching)
    monkeypatch.setattr(cancellation_module, "_cancel_query_ids", cancel)
    monkeypatch.setattr(cancellation_module.time, "sleep", lambda _seconds: None)

    cancellation_module.cancel_scope_queries(scope, timeout=1)

    assert sorted(cancelled) == [
        ("gp", ("gp-query",)),
        ("trino", ("trino-query",)),
    ]
    assert calls == {"gp": 2, "trino": 2}


def test_cancel_scope_queries_deadline_warns_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scope = cancellation_module.SqlCancellationScope()
    scope.register_alias("gp")
    release = threading.Event()

    def blocked(*args: Any) -> tuple[str, tuple[int | str, ...], str | None]:
        release.wait(timeout=1)
        return "gp", (), None

    monkeypatch.setattr(cancellation_module, "_cancel_alias_until_clear", blocked)
    cancellation_module.cancel_scope_queries(scope, timeout=0.01)
    release.set()

    output = capsys.readouterr().out
    assert "cancellation did not finish within 0.01 seconds" in output
    assert "[gp] [cancel]" in output


def test_cancel_scope_queries_reports_errors_and_remaining_ids(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scope = cancellation_module.SqlCancellationScope()
    scope.register_alias("gp")
    scope.register_alias("trino")

    def result(
        connection_key: str,
        _marker: str,
        _deadline: float,
    ) -> tuple[str, tuple[int | str, ...], str | None]:
        if connection_key == "gp":
            return connection_key, (), "permission denied"
        return connection_key, ("query-1",), None

    monkeypatch.setattr(cancellation_module, "_cancel_alias_until_clear", result)
    cancellation_module.cancel_scope_queries(scope, timeout=1)

    output = capsys.readouterr().out
    assert "permission denied" in output
    assert "query-1" in output


def test_cancel_alias_until_clear_reports_deadline_and_driver_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cancellation_module,
        "_matching_query_ids",
        lambda *_args: [7],
    )
    expired = cancellation_module._cancel_alias_until_clear("gp", "marker", -1)
    assert expired == ("gp", (7,), None)
    inspection_error = RuntimeError("cannot inspect")

    def fail(*_args: Any) -> list[int | str]:
        raise inspection_error

    monkeypatch.setattr(cancellation_module, "_matching_query_ids", fail)
    failed = cancellation_module._cancel_alias_until_clear(
        "gp",
        "marker",
        time.monotonic() + 1,
    )
    assert failed == ("gp", (), "RuntimeError: cannot inspect")


def test_cancel_alias_until_clear_polls_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter([0.0, 0.0, 0.0])
    matches = iter([[7], []])
    cancelled: list[tuple[str, list[int | str]]] = []
    monkeypatch.setattr(cancellation_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        cancellation_module,
        "_matching_query_ids",
        lambda *_args: next(matches),
    )
    monkeypatch.setattr(
        cancellation_module,
        "_cancel_query_ids",
        lambda connection_key, query_ids: cancelled.append((connection_key, query_ids)),
    )
    monkeypatch.setattr(cancellation_module.time, "sleep", lambda _seconds: None)

    result = cancellation_module._cancel_alias_until_clear("gp", "marker", 1.0)

    assert result == ("gp", (), None)
    assert cancelled == [("gp", [7])]


def test_matching_query_ids_handles_missing_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        show_queries_module,
        "show_queries",
        lambda *args, **kwargs: pd.DataFrame({"state": []}),
    )
    assert cancellation_module._matching_query_ids("gp", "marker") == []


def test_cancel_query_ids_uses_bounded_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cancel_queries_module,
        "cancel_queries",
        lambda connection_key, query_ids, **kwargs: calls.append(
            {"connection_key": connection_key, "query_ids": query_ids, **kwargs}
        ),
    )

    cancellation_module._cancel_query_ids("trino", [1, 2, 3, 4, 5])

    assert calls == [
        {
            "connection_key": "trino",
            "query_ids": [1, 2, 3, 4, 5],
            "concurrency": 4,
            "retry_cnt": 1,
            "timeout_increment": 0,
        }
    ]


class FakeExecutor:
    def __init__(self, *, reject_cancel_futures: bool = False) -> None:
        self.reject_cancel_futures = reject_cancel_futures
        self.calls: list[tuple[bool, bool | None]] = []

    def shutdown(
        self,
        *,
        wait: bool,
        cancel_futures: bool | None = None,
    ) -> None:
        self.calls.append((wait, cancel_futures))
        if self.reject_cancel_futures and cancel_futures is not None:
            raise TypeError


def test_shutdown_executor_falls_back_for_python_38_signature() -> None:
    executor = FakeExecutor(reject_cancel_futures=True)

    cancellation_module.shutdown_executor(  # type: ignore[arg-type]
        executor,
        wait=False,
        cancel_futures=True,
    )

    assert executor.calls == [(False, True), (False, None)]


@pytest.mark.parametrize(
    "function_name",
    ["get_sql_connection", "get_ch_connection_for_host"],
)
def test_connection_open_closes_if_batch_is_cancelled_during_connect(
    monkeypatch: pytest.MonkeyPatch,
    function_name: str,
) -> None:
    connection = type("Connection", (), {"close": lambda self: setattr(self, "closed", True)})()
    connection.closed = False
    backend = type(
        "Backend",
        (),
        {"open_connection": lambda self, *args, **kwargs: connection},
    )()
    checks = 0
    cancellation_error = cancellation_module.AsyncSqlCancelled("cancelled")

    def cancel_after_open() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise cancellation_error

    monkeypatch.setattr(connection_module, "get_backend", lambda _backend: backend)
    monkeypatch.setattr(connection_module, "raise_if_cancelled", cancel_after_open)

    function = getattr(connection_module, function_name)
    args = ("ch", "other-host") if function_name == "get_ch_connection_for_host" else ("ch",)
    with pytest.raises(cancellation_module.AsyncSqlCancelled):
        function(*args)

    assert connection.closed


def test_to_thread_uses_native_asyncio_helper() -> None:
    assert asyncio.run(async_module._to_thread(lambda: "done")) == "done"


def test_interrupt_result_helper_reraises_interrupt() -> None:
    error = KeyboardInterrupt("stop")
    with pytest.raises(KeyboardInterrupt) as caught:
        async_module._raise_interrupt_result(error)
    assert caught.value is error


def test_existing_loop_bridge_handles_interrupt_before_runner_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = KeyboardInterrupt("early interrupt")

    class InterruptingQueue:
        def __init__(self, maxsize: int) -> None:
            assert maxsize == 1

        def put(self, _item: Any) -> None:
            pass

        def get(self) -> Any:
            raise error

    class UnstartedThread:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def join(self, timeout: float | None = None) -> None:
            assert timeout == 10

    monkeypatch.setattr(async_module, "Queue", InterruptingQueue)
    monkeypatch.setattr(async_module, "Thread", UnstartedThread)

    with pytest.raises(KeyboardInterrupt) as caught:
        async_module._run_coroutine_sync_in_thread(lambda: asyncio.sleep(0))

    assert caught.value is error


def test_retry_wait_is_interruptible_inside_async_sql_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = cancellation_module.SqlCancellationScope()
    waits: list[float] = []
    attempts: list[int] = []
    retry_error = RuntimeError("retry")

    def operation(attempt: int) -> str:
        attempts.append(attempt)
        if attempt == 1:
            raise retry_error
        return "ok"

    monkeypatch.setattr(retry_module, "wait_or_raise_if_cancelled", waits.append)
    with cancellation_module.activate_cancellation_scope(scope):
        result = retry_module.run_with_retry(
            "interruptible retry",
            retry_cnt=2,
            timeout_increment=3,
            operation=operation,
        )

    assert result == "ok"
    assert attempts == [1, 2]
    assert waits == [3]


def test_async_impl_cancellation_cancels_pending_pipeline_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = cancellation_module.SqlCancellationScope()

    async def cancel_batch() -> None:
        started = asyncio.Event()

        async def wait_forever(_context: Any) -> None:
            started.set()
            await asyncio.Event().wait()

        with cancellation_module.activate_cancellation_scope(scope):
            batch = asyncio.create_task(
                async_module._async_sql_impl(
                    [
                        {
                            "name": "pending",
                            "type": "custom_sql_pipeline",
                            "steps": [wait_forever],
                        }
                    ]
                )
            )
        await started.wait()
        batch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await batch

    monkeypatch.setattr(async_module, "cancel_scope_queries", lambda _scope: None)
    asyncio.run(cancel_batch())
    assert scope.cancelled


@pytest.mark.parametrize("fail_fast", [True, False])
def test_async_sql_keyboard_interrupt_stops_queued_tasks(
    monkeypatch: pytest.MonkeyPatch,
    fail_fast: bool,
) -> None:
    started = threading.Event()
    calls: list[str] = []
    cleanup_scopes: list[Any] = []
    error = KeyboardInterrupt("stop batch")

    def fake_read_sql(**kwargs: Any) -> str:
        query = kwargs["query"]
        calls.append(query)
        if query == "long":
            started.set()
            while True:
                cancellation_module.raise_if_cancelled()
                time.sleep(0.001)
        return query

    def interrupt(_context: Any) -> None:
        assert started.wait(timeout=1)
        raise error

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)
    monkeypatch.setattr(
        async_module,
        "cancel_scope_queries",
        cleanup_scopes.append,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        async_module.async_sql(
            [
                {"name": "running", "type": "read", "db_key": "gp", "query": "long"},
                {
                    "name": "interrupt",
                    "type": "custom_sql_pipeline",
                    "steps": [interrupt],
                },
                {"name": "queued", "type": "read", "db_key": "gp", "query": "queued"},
            ],
            concurrency=2,
            fail_fast=fail_fast,
        )

    assert caught.value is error
    assert calls == ["long"]
    assert len(cleanup_scopes) == 1
    assert cleanup_scopes[0].cancelled


def test_existing_loop_bridge_propagates_caller_interrupt_to_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = cancellation_module.SqlCancellationScope()
    error = KeyboardInterrupt("positron interrupt")
    coroutine_cancelled = threading.Event()

    class InterruptingQueue:
        def __init__(self, maxsize: int) -> None:
            assert maxsize == 1
            self.item: Any = None

        def put(self, item: Any) -> None:
            self.item = item

        def get(self) -> Any:
            time.sleep(0.02)
            raise error

    async def wait_forever() -> dict[str, Any]:
        try:
            await asyncio.Event().wait()
        finally:
            coroutine_cancelled.set()

    monkeypatch.setattr(async_module, "Queue", InterruptingQueue)

    with pytest.raises(KeyboardInterrupt) as caught:
        async_module._run_coroutine_sync_in_thread(
            wait_forever,
            cancellation_scope=scope,
        )

    assert caught.value is error
    assert scope.cancelled
    assert coroutine_cancelled.wait(timeout=1)
