from __future__ import annotations

from tests.sql._support.transfer_runtime import (
    Any,
    BoundedConnectionCloseError,
    BoundedConnectionManager,
    LazyKeyedRuntime,
    ThreadPoolExecutor,
    _Connection,
    _ConnectionWithoutCancel,
    _queued_batch,
    _task,
    lazy_keyed_runtime,
    pytest,
    queue,
    threading,
    time,
)


def test_connection_pool_concurrent_operations_never_exceed_capacity() -> None:
    release = threading.Event()
    saturated = threading.Event()
    state_lock = threading.Lock()
    active = 0
    high_water = 0
    opened: list[_Connection] = []

    def open_connection(_key: str) -> _Connection:
        connection = _Connection()
        opened.append(connection)
        return connection

    def operation(ref: dict[str, Any]) -> int:
        nonlocal active, high_water
        with state_lock:
            active += 1
            high_water = max(high_water, active)
            if active == 2:
                saturated.set()
        assert release.wait(2)
        with state_lock:
            active -= 1
        return id(ref["connection"])

    manager = BoundedConnectionManager(
        "target",
        2,
        role="concurrent pool",
        open_connection=open_connection,
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(manager.run, "write", operation) for _ in range(4)]
        assert saturated.wait(2)
        time.sleep(0.15)
        assert len(opened) == 2
        assert high_water == 2
        release.set()
        connection_ids = [future.result(timeout=2) for future in futures]

    assert set(connection_ids) == {id(connection) for connection in opened}
    assert manager.high_water_mark == 2
    manager.close()
    assert [connection.close_calls for connection in opened] == [1, 1]


def test_interrupt_closes_active_connection_and_allows_explicit_cleanup_resume() -> None:
    operation_started = threading.Event()
    release_operation = threading.Event()
    opened: list[_Connection] = []

    def open_connection(_key: str) -> _Connection:
        connection = _Connection()
        opened.append(connection)
        return connection

    def operation(_ref: dict[str, Any]) -> None:
        operation_started.set()
        assert release_operation.wait(2)

    manager = BoundedConnectionManager(
        "source",
        1,
        role="interrupt pool",
        open_connection=open_connection,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(manager.run, "read", operation)
        assert operation_started.wait(2)
        manager.interrupt_active()
        assert opened[0].cancel_calls == 1
        assert opened[0].close_calls == 1
        with pytest.raises(RuntimeError, match="active connection leases"):
            manager.resume_for_cleanup()
        with pytest.raises(RuntimeError, match="was interrupted"), manager.lease():
            pass
        with pytest.raises(RuntimeError, match="was interrupted"):
            manager.ensure_connection("source", manager._refs[0])
        with pytest.raises(RuntimeError, match="Specialized Connection") as exc_info:
            manager._begin_open("specialized connection")
        assert exc_info.value.analytics_toolkit_sql_retry_safe is False
        release_operation.set()
        assert future.result(timeout=2) is None

    manager.resume_for_cleanup()
    with manager.lease():
        pass
    assert len(opened) == 2
    manager.close()


def test_interrupt_closes_active_connection_without_optional_cancel_method() -> None:
    operation_started = threading.Event()
    release_operation = threading.Event()
    connection = _ConnectionWithoutCancel()
    manager = BoundedConnectionManager(
        "source",
        1,
        role="no cancel pool",
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

    assert connection.close_calls == 1
    manager.resume_for_cleanup()
    manager.close()


def test_interrupt_retains_unclosed_connection_until_resume_can_prove_close() -> None:
    operation_started = threading.Event()
    release_operation = threading.Event()
    connection = _Connection(close_failures=1, cancel_fails=True)
    manager = BoundedConnectionManager(
        "source",
        1,
        role="interrupted close retry pool",
        open_connection=lambda _key: connection,
    )

    def operation(_ref: dict[str, Any]) -> None:
        operation_started.set()
        assert release_operation.wait(2)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(manager.run, "read", operation)
        assert operation_started.wait(2)
        manager.interrupt_active()
        assert connection.cancel_calls == 1
        assert connection.close_calls == 1
        release_operation.set()
        future.result(timeout=2)

    manager.resume_for_cleanup()
    assert connection.close_calls == 2
    with manager.lease() as ref:
        assert ref["connection"] is connection
    manager.close()
    assert connection.close_calls == 3


def test_queue_helpers_complete_successfully_and_get_honors_cancellation() -> None:
    runtime = LazyKeyedRuntime([], read_workers=1, write_workers=1)
    batch_queue: queue.Queue[Any] = queue.Queue(maxsize=1)
    batch = _queued_batch(_task(0))
    lazy_keyed_runtime.put_batch_with_cancellation(batch_queue, batch, runtime)
    assert batch.queued_at is not None
    assert lazy_keyed_runtime.get_with_cancellation(batch_queue, runtime) is batch

    generic_queue: queue.Queue[Any] = queue.Queue(maxsize=1)
    lazy_keyed_runtime.put_with_cancellation(generic_queue, "value", runtime)
    assert lazy_keyed_runtime.get_with_cancellation(generic_queue, runtime) == "value"

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lazy_keyed_runtime.get_with_cancellation, generic_queue, runtime)
        time.sleep(0.15)
        runtime.cancellation.set()
        with pytest.raises(RuntimeError, match="queue wait was cancelled"):
            future.result(timeout=2)


@pytest.mark.parametrize("rejected_close_fails", [False, True])
def test_replacement_open_race_never_accepts_connection_after_interrupt(
    rejected_close_fails: bool,
) -> None:
    replacement_started = threading.Event()
    release_replacement = threading.Event()
    opened: list[_Connection] = []

    def open_connection(_key: str) -> _Connection:
        is_replacement = bool(opened)
        connection = _Connection(close_failures=int(rejected_close_fails and is_replacement))
        opened.append(connection)
        if len(opened) == 2:
            replacement_started.set()
            assert release_replacement.wait(2)
        return connection

    manager = BoundedConnectionManager(
        "source",
        1,
        role="replacement race pool",
        open_connection=open_connection,
    )

    def replace() -> None:
        with manager.lease() as ref:
            manager.replace_connection("source", ref)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(replace)
        assert replacement_started.wait(2)
        manager.interrupt_active()
        release_replacement.set()
        expected_error = BoundedConnectionCloseError if rejected_close_fails else RuntimeError
        with pytest.raises(expected_error) as exc_info:
            future.result(timeout=2)

    if rejected_close_fails:
        assert "cancelled replacement" in str(exc_info.value)
        manager.resume_for_cleanup()
    else:
        assert "replacement" in str(exc_info.value)
        assert exc_info.value.analytics_toolkit_sql_retry_safe is False
        manager.resume_for_cleanup()
    manager.close()
    assert opened[0].close_calls == 1
    assert opened[1].close_calls == (2 if rejected_close_fails else 1)


def test_waiting_connection_lease_honors_cancellation() -> None:
    manager = BoundedConnectionManager(
        "source",
        1,
        role="cancellable pool",
        open_connection=lambda _key: _Connection(),
    )
    cancellation = threading.Event()

    def wait_for_lease() -> None:
        with manager.lease(cancellation=cancellation):
            pytest.fail("cancelled waiter acquired a connection")

    with manager.lease(), ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(wait_for_lease)
        time.sleep(0.15)
        cancellation.set()
        with pytest.raises(RuntimeError, match="lease cancelled"):
            future.result(timeout=2)
    manager.close()
