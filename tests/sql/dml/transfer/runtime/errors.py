from __future__ import annotations

from tests.sql._support.transfer_runtime import (
    Any,
    BoundedConnectionCloseError,
    BoundedConnectionManager,
    LazyKeyedRuntime,
    QueuedKeyBatch,
    ThreadPoolExecutor,
    _CloseError,
    _Connection,
    _manager_with_close_failure,
    _NoNoteError,
    _queued_batch,
    _StateRejectingError,
    _task,
    lazy_keyed_runtime,
    pytest,
    queue,
    threading,
    time,
)


def test_close_aggregates_all_driver_failures() -> None:
    connections = [_Connection(close_failures=10), _Connection(close_failures=10)]
    manager = BoundedConnectionManager(
        "target",
        2,
        role="aggregate close pool",
        open_connection=lambda _key: connections.pop(),
    )
    with manager.lease(), manager.lease():
        pass
    with pytest.raises(BoundedConnectionCloseError, match="2 connection") as exc_info:
        manager.close()
    assert isinstance(exc_info.value.__cause__, _CloseError)
    assert manager._open_count == 2


def test_close_preserving_marks_original_error_nonretryable() -> None:
    manager = _manager_with_close_failure()
    original = ValueError("original operation failure")

    assert manager.close_preserving(original) is None

    assert original.analytics_toolkit_sql_retry_safe is False
    notes = getattr(original, "__notes__", [])
    if callable(getattr(original, "add_note", None)):
        assert notes == [
            "Bounded strict close pool cleanup also failed: BoundedConnectionCloseError"
        ]


def test_close_preserving_supports_errors_without_add_note() -> None:
    manager = _manager_with_close_failure("no note pool")
    original = _NoNoteError("no note support")

    assert manager.close_preserving(original) is None
    assert original.analytics_toolkit_sql_retry_safe is False


def test_connection_pool_reuses_refs_and_exposes_retry_callbacks() -> None:
    opened: list[_Connection] = []

    def open_connection(_key: str) -> _Connection:
        connection = _Connection()
        opened.append(connection)
        return connection

    manager = BoundedConnectionManager(
        "source",
        1,
        role="reuse pool",
        open_connection=open_connection,
    )
    assert manager.resume_for_cleanup() is None

    first_id = manager.run(
        "read",
        lambda ref: (
            id(ref),
            callable(ref["bounded_replace_connection"]),
            callable(ref["bounded_ensure_connection"]),
        ),
    )
    with manager.lease() as ref:
        manager.ensure_connection("source", ref)
        second_id = id(ref)

    assert first_id == (second_id, True, True)
    assert len(opened) == 1
    assert manager.high_water_mark == 1
    manager.close()
    assert opened[0].close_calls == 1
    with pytest.raises(RuntimeError, match="manager is closed"), manager.lease():
        pass
    with pytest.raises(RuntimeError, match="manager is closed"):
        manager.resume_for_cleanup()


def test_lazy_runtime_preserves_first_error_and_suppresses_callback_errors() -> None:
    runtime = LazyKeyedRuntime([], read_workers=1, write_workers=1)
    callbacks: list[str] = []

    def successful_callback() -> None:
        callbacks.append("called")

    def failing_callback() -> None:
        callbacks.append("failed")
        raise RuntimeError

    runtime.add_failure_callback(successful_callback)
    runtime.add_failure_callback(failing_callback)
    first = ValueError("first")
    runtime.fail(first)
    runtime.fail(RuntimeError("second"))

    assert callbacks == ["called", "failed"]
    assert runtime.first_error is first
    assert runtime.cancellation.is_set()
    with pytest.raises(ValueError, match="first") as exc_info:
        runtime.raise_first_error()
    assert exc_info.value is first

    untouched = LazyKeyedRuntime([], read_workers=1, write_workers=1)
    assert untouched.raise_first_error() is None


@pytest.mark.parametrize(
    ("helper", "message"),
    [
        (lazy_keyed_runtime.put_with_cancellation, "queue handoff was cancelled"),
        (lazy_keyed_runtime.put_batch_with_cancellation, "Batch handoff was cancelled"),
    ],
)
def test_queue_put_helpers_retry_full_queue_then_honor_cancellation(
    helper: Any,
    message: str,
) -> None:
    runtime = LazyKeyedRuntime([], read_workers=1, write_workers=1)
    destination: queue.Queue[Any] = queue.Queue(maxsize=1)
    destination.put_nowait("occupied")
    item: Any = (
        _queued_batch(_task(0))
        if helper is lazy_keyed_runtime.put_batch_with_cancellation
        else "next"
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(helper, destination, item, runtime)
        time.sleep(0.15)
        runtime.cancellation.set()
        with pytest.raises(RuntimeError, match=message):
            future.result(timeout=2)

    if isinstance(item, QueuedKeyBatch):
        assert item.queued_at is not None


def test_rejected_open_close_failure_never_overwrites_tracked_connection() -> None:
    tracked = _Connection()
    manager = BoundedConnectionManager(
        "source",
        1,
        role="tracked rejection pool",
        open_connection=lambda _key: tracked,
    )
    with manager.lease() as ref:
        rejected = _Connection(close_failures=1)
        with pytest.raises(BoundedConnectionCloseError, match="cancelled direct test"):
            manager._reject_opened_connection(rejected, ref, action="direct test")
        assert ref["connection"] is tracked

    assert rejected.close_calls == 1
    assert manager.high_water_mark == 1
    manager.close()
    assert tracked.close_calls == 1


def test_resume_cleanup_aggregates_persistent_close_failure() -> None:
    operation_started = threading.Event()
    release_operation = threading.Event()
    connection = _Connection(close_failures=10)
    manager = BoundedConnectionManager(
        "source",
        1,
        role="persistent interrupted close pool",
        open_connection=lambda _key: connection,
    )

    def operation(_ref: dict[str, Any]) -> None:
        operation_started.set()
        assert release_operation.wait(2)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(manager.run, "read", operation)
        assert operation_started.wait(2)
        manager.interrupt_active()
        release_operation.set()
        future.result(timeout=2)

    with pytest.raises(BoundedConnectionCloseError, match="cleanup will not open"):
        manager.resume_for_cleanup()
    assert connection.close_calls == 2
    with pytest.raises(BoundedConnectionCloseError):
        manager.close()


def test_specialized_cleanup_preserves_error_without_add_note() -> None:
    connection = _Connection(close_failures=10)
    manager = BoundedConnectionManager(
        "target",
        1,
        role="specialized no-note pool",
        open_connection=lambda _key: pytest.fail("default connection should stay lazy"),
    )
    original = _NoNoteError("specialized no-note failure")

    def fail_operation(_connection: Any) -> None:
        raise original

    with pytest.raises(_NoNoteError, match="specialized no-note failure") as exc_info:
        manager.run_with_connection("host cleanup", lambda: connection, fail_operation)

    assert exc_info.value is original
    assert original.analytics_toolkit_sql_retry_safe is False


@pytest.mark.parametrize("mutable_error", [True, False])
def test_specialized_connection_preserves_operation_error_when_cleanup_also_fails(
    mutable_error: bool,
) -> None:
    connection = _Connection(close_failures=10)
    manager = BoundedConnectionManager(
        "target",
        1,
        role="specialized error pool",
        open_connection=lambda _key: pytest.fail("default connection should stay lazy"),
    )
    original: BaseException = (
        ValueError("specialized operation failed")
        if mutable_error
        else _StateRejectingError("immutable specialized error")
    )

    def fail_operation(_connection: Any) -> None:
        raise original

    if mutable_error:
        with pytest.raises(ValueError, match="specialized operation failed") as exc_info:
            manager.run_with_connection("host cleanup", lambda: connection, fail_operation)
        assert exc_info.value is original
        assert original.analytics_toolkit_sql_retry_safe is False
        notes = getattr(original, "__notes__", [])
        if callable(getattr(original, "add_note", None)):
            assert notes == [
                "Bounded specialized target connection cleanup also failed: "
                "BoundedConnectionCloseError"
            ]
    else:
        with pytest.raises(BoundedConnectionCloseError) as exc_info:
            manager.run_with_connection("host cleanup", lambda: connection, fail_operation)
        assert exc_info.value.__cause__ is original


def test_specialized_connection_success_and_cleanup_failure_paths() -> None:
    successful = _Connection()
    manager = BoundedConnectionManager(
        "target",
        1,
        role="specialized pool",
        open_connection=lambda _key: pytest.fail("default connection should stay lazy"),
    )
    result = manager.run_with_connection(
        "host cleanup",
        lambda: successful,
        lambda connection: connection,
    )
    assert result is successful
    assert successful.close_calls == 1
    assert manager.high_water_mark == 1

    close_failure = _Connection(close_failures=10)
    with pytest.raises(BoundedConnectionCloseError, match="specialized connection cleanup"):
        manager.run_with_connection(
            "host cleanup",
            lambda: close_failure,
            lambda _connection: "done",
        )
    assert close_failure.close_calls == 1
