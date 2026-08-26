from __future__ import annotations

from tests.sql._support.transfer_runtime import (
    BoundedConnectionManager,
    LazyKeyedRuntime,
    SimpleNamespace,
    ThreadPoolExecutor,
    VerifiedKey,
    _Connection,
    _queued_batch,
    _slice,
    _task,
    lazy_keyed_runtime,
    pytest,
    threading,
    time,
)


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
