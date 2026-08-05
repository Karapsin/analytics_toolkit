from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit.sql.dml.transfer.flow import lazy_keyed_runtime
from analytics_toolkit.sql.dml.transfer.flow.lazy_keyed_runtime import (
    LazyKeyedRuntime,
    QueuedKeyBatch,
    ReadyKeyTask,
    VerifiedKey,
)
from analytics_toolkit.sql.dml.transfer.runtime.connection_pool import (
    BoundedConnectionCloseError,
    BoundedConnectionManager,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch, TransferSlice


class _CloseError(OSError):
    pass


class _OpenError(OSError):
    pass


class _Connection:
    def __init__(self, *, close_failures: int = 0, cancel_fails: bool = False) -> None:
        self.close_failures = close_failures
        self.cancel_fails = cancel_fails
        self.close_calls = 0
        self.cancel_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.close_calls <= self.close_failures:
            raise _CloseError

    def cancel(self) -> None:
        self.cancel_calls += 1
        if self.cancel_fails:
            raise RuntimeError


class _ConnectionWithoutCancel:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _StateRejectingError(Exception):
    def __getattribute__(self, name: str) -> Any:
        if name == "__dict__":
            raise AttributeError
        return super().__getattribute__(name)


class _NoNoteError(Exception):
    add_note = None


@pytest.mark.parametrize("capacity", [True, False, 0, -1, 1.5, "2"])
def test_connection_pool_rejects_nonpositive_or_non_integer_capacity(capacity: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        BoundedConnectionManager("source", capacity, role="validation pool")


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


def test_replace_failure_leaves_ref_reopenable_and_validates_ownership() -> None:
    attempts = 0
    opened: list[_Connection] = []

    def open_connection(_key: str) -> _Connection:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise _OpenError
        connection = _Connection()
        opened.append(connection)
        return connection

    manager = BoundedConnectionManager(
        "source",
        1,
        role="replacement pool",
        open_connection=open_connection,
    )
    with manager.lease() as ref:
        with pytest.raises(RuntimeError, match="does not belong"):
            manager.replace_connection("other", ref)
        with pytest.raises(RuntimeError, match="does not belong"):
            manager.replace_connection("source", {})
        with pytest.raises(RuntimeError, match="does not belong"):
            manager.ensure_connection("other", ref)
        with pytest.raises(RuntimeError, match="does not belong"):
            manager.ensure_connection("source", {})
        with pytest.raises(RuntimeError, match="Could not replace"):
            manager.replace_connection("source", ref)
        assert "connection" not in ref
        manager.ensure_connection("source", ref)
        manager.ensure_connection("source", ref)

    assert attempts == 3
    assert opened[0].close_calls == 1
    manager.close()
    assert opened[1].close_calls == 1


def test_replace_is_nonretryable_after_manager_interruption() -> None:
    manager = BoundedConnectionManager(
        "source",
        1,
        role="interrupted replacement pool",
        open_connection=lambda _key: _Connection(),
    )
    ref = manager._refs[0]
    manager.interrupt_active()

    with pytest.raises(RuntimeError, match=r"replacement.*cancelled") as exc_info:
        manager.replace_connection("source", ref)

    assert exc_info.value.analytics_toolkit_sql_retry_safe is False
    manager.resume_for_cleanup()
    manager.close()


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


def test_close_waits_for_inflight_open_and_rejects_late_connection() -> None:
    open_started = threading.Event()
    release_open = threading.Event()
    connection = _Connection()

    def open_connection(_key: str) -> _Connection:
        open_started.set()
        assert release_open.wait(2)
        return connection

    manager = BoundedConnectionManager(
        "source",
        1,
        role="inflight close pool",
        open_connection=open_connection,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        lease_future = executor.submit(manager.run, "read", lambda _ref: None)
        assert open_started.wait(2)
        close_future = executor.submit(manager.close)
        time.sleep(0.05)
        assert not close_future.done()
        release_open.set()
        with pytest.raises(RuntimeError, match="opening was cancelled") as exc_info:
            lease_future.result(timeout=2)
        assert exc_info.value.analytics_toolkit_sql_retry_safe is False
        assert close_future.result(timeout=2) is None

    assert connection.close_calls == 1


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


def _manager_with_close_failure(role: str = "strict close pool") -> BoundedConnectionManager:
    manager = BoundedConnectionManager(
        "target",
        1,
        role=role,
        open_connection=lambda _key: _Connection(close_failures=10),
    )
    with manager.lease():
        pass
    return manager


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


def test_close_preserving_handles_missing_error_and_nonmutable_error_state() -> None:
    without_original = _manager_with_close_failure("no original pool")
    with pytest.raises(BoundedConnectionCloseError):
        without_original.close_preserving(None)

    nonmutable_original = _StateRejectingError("immutable exception state")
    nonmutable = _manager_with_close_failure("immutable error pool")
    with pytest.raises(BoundedConnectionCloseError) as exc_info:
        nonmutable.close_preserving(nonmutable_original)
    assert exc_info.value.__cause__ is nonmutable_original


def test_close_preserving_supports_errors_without_add_note() -> None:
    manager = _manager_with_close_failure("no note pool")
    original = _NoNoteError("no note support")

    assert manager.close_preserving(original) is None
    assert original.analytics_toolkit_sql_retry_safe is False


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


def _slice(index: int) -> TransferSlice:
    return TransferSlice(index, (index,), "", f"SELECT {index}", f"key={index}")


def _task(index: int, table: str = "source.stage") -> ReadyKeyTask:
    return ReadyKeyTask(
        transfer_slice=_slice(index),
        source_stage=table,
        expected_rows=1,
        tag=f"[slice={index + 1}/2]",
        materialized_at=time.monotonic(),
    )


def _queued_batch(task: ReadyKeyTask) -> QueuedKeyBatch:
    batch = RowBatch(["id"], [(1,)])
    queued = QueuedKeyBatch(
        task=task,
        batch_index=1,
        start_ordinal=1,
        stop_ordinal=2,
        batch=batch,
        read_started_at=1.0,
        read_completed_at=2.0,
        approximate_memory_bytes=batch.approx_memory_bytes(),
    )
    assert queued.logical_id == (task.transfer_slice.index, 1, 1, 2)
    return queued


def test_lazy_runtime_state_transitions_and_duplicate_guards() -> None:
    runtime = LazyKeyedRuntime([_slice(0), _slice(1)], read_workers=1, write_workers=2)
    assert runtime.first_error is None
    assert runtime.claim_pending() == _slice(0)
    assert runtime.claim_pending() == _slice(1)
    assert runtime.claim_pending() is None

    task = _task(0)
    assert runtime.live_stage_credits.acquire(blocking=False)
    runtime.reserve_source_stage(task.source_stage)
    with pytest.raises(RuntimeError, match="reserved twice"):
        runtime.reserve_source_stage(task.source_stage)
    with pytest.raises(RuntimeError, match="not reserved"):
        runtime.publish_source_stage(_task(1, "source.unknown"))
    runtime.publish_source_stage(task)
    assert runtime.live_source_stage_count == 1
    assert runtime.source_stage_tables == [task.source_stage]
    assert runtime.source_stage_entries == {task.source_stage: task}
    runtime.mark_source_stage_dropped("source.absent")
    runtime.mark_source_stage_dropped(task.source_stage)
    assert runtime.source_stages_dropped == 1
    assert runtime.live_source_stage_count == 0

    runtime.register_target_stage(0, "target.writer_0")
    runtime.register_target_stage(0, "target.writer_0")
    with pytest.raises(RuntimeError, match="created two stages"):
        runtime.register_target_stage(0, "target.other")
    runtime.register_target_stage_candidate("target.writer_0")
    runtime.register_target_stage_candidate("target.writer_0")
    target_stages = runtime.target_stages
    target_stages.clear()
    candidates = runtime.target_stage_candidates
    candidates.clear()
    assert runtime.target_stages == {0: "target.writer_0"}
    assert runtime.target_stage_candidates == {"target.writer_0"}

    logical_id = (0, 1, 1, 2)
    runtime.mark_batch_success(logical_id)
    with pytest.raises(RuntimeError, match="committed twice"):
        runtime.mark_batch_success(logical_id)
    checkpoint = VerifiedKey(0, 1, 1, "target.writer_0")
    runtime.mark_verified(checkpoint)
    with pytest.raises(RuntimeError, match="verified twice"):
        runtime.mark_verified(checkpoint)
    copied = runtime.verified
    copied.clear()
    assert runtime.verified == {0: checkpoint}


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


def test_attempt_metadata_is_frozen_from_mutable_inputs() -> None:
    source_columns = ["id"]
    source_types = {"id": "bigint"}
    stage_types = {"id": "BIGINT"}
    internal = SimpleNamespace(transfer_id="transfer_id")
    metadata = lazy_keyed_runtime.freeze_attempt_metadata(
        source_columns=source_columns,
        source_column_types=source_types,
        stage_column_types=stage_types,
        internal_columns=internal,
    )
    source_columns.append("late")
    source_types["late"] = "text"
    stage_types["late"] = "TEXT"

    assert metadata.source_columns == ("id",)
    assert dict(metadata.source_column_types) == {"id": "bigint"}
    assert dict(metadata.stage_column_types or {}) == {"id": "BIGINT"}
    with pytest.raises(TypeError):
        metadata.source_column_types["late"] = "text"  # type: ignore[index]

    without_stage_types = lazy_keyed_runtime.freeze_attempt_metadata(
        source_columns=[],
        source_column_types={},
        stage_column_types=None,
        internal_columns=internal,
    )
    assert without_stage_types.stage_column_types is None


def test_lazy_batch_sizer_divides_memory_across_resident_writer_slots() -> None:
    options = SimpleNamespace(
        transfer_concurrency=SimpleNamespace(effective_write=2),
        adaptive_batch_size=True,
        batch_size=10,
        min_batch_size=1,
        max_batch_size=100,
        target_batch_seconds=None,
        min_batch_seconds=None,
        max_batch_seconds=None,
        target_rows_per_second=False,
        target_rows_per_second_window=5,
        target_rows_per_second_deadband=0.1,
        adaptive_batch_size_step=0.2,
        target_batch_memory_bytes=9,
        min_batch_memory_bytes=3,
        max_batch_memory_bytes=None,
    )

    sizer = lazy_keyed_runtime.make_batch_sizer(options)

    assert sizer.target_memory_bytes == 2
    assert sizer.min_target_memory_bytes == 1
    assert sizer.max_target_memory_bytes is None


def test_assignment_slot_and_release_helpers_honor_state() -> None:
    runtime = LazyKeyedRuntime([], read_workers=1, write_workers=1)
    assigned = _task(0)
    assigned.assignment.set()
    assert lazy_keyed_runtime.wait_for_assignment(assigned, runtime) is None

    cancelled = _task(1)
    runtime.cancellation.set()
    with pytest.raises(RuntimeError, match="assignment was cancelled"):
        lazy_keyed_runtime.wait_for_assignment(cancelled, runtime)

    active = LazyKeyedRuntime([], read_workers=1, write_workers=1)
    slot = threading.BoundedSemaphore(1)
    lazy_keyed_runtime.acquire_batch_slot_with_cancellation(slot, active)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            lazy_keyed_runtime.acquire_batch_slot_with_cancellation,
            slot,
            active,
        )
        time.sleep(0.15)
        slot.release()
        assert future.result(timeout=2) is None

    active.cancellation.set()
    with pytest.raises(RuntimeError, match="prefetch wait was cancelled"):
        lazy_keyed_runtime.acquire_batch_slot_with_cancellation(slot, active)

    batch = _queued_batch(_task(0))
    batch.prefetch_slot = slot
    lazy_keyed_runtime.release_queued_batch_slot(batch)
    assert batch.prefetch_slot is None
    lazy_keyed_runtime.release_queued_batch_slot(batch)
    assert slot.acquire(blocking=False)


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
