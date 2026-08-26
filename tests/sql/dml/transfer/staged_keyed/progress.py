from __future__ import annotations

from tests.sql._support.staged_keyed import (
    Any,
    LazyKeyedRuntime,
    TransferProgressTracker,
    _concurrency,
    _options,
    _ready_task,
    staged_keyed_logging,
    staged_keyed_pipeline,
)


def test_keyed_progress_log_messages_are_tag_first_and_phase_complete(
    monkeypatch: Any,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    transfer_slice = (options.transfer_slices or [])[0]
    task = _ready_task(transfer_slice, "source.stage", 20)
    now = [0.0]
    tracker = TransferProgressTracker(
        total_key_count=2,
        active_writers=1,
        clock=lambda: now[0],
    )
    tracker.start_key(transfer_slice.index)
    tracker.materialize_key(transfer_slice.index, 20)
    tracker.assign_key(transfer_slice.index, 0)
    messages: list[str] = []
    monkeypatch.setattr(staged_keyed_logging, "time_print", messages.append)

    staged_keyed_logging.log_pipeline_start(
        options,
        LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=1),
    )
    now[0] = 2.0
    first = tracker.commit_batch(
        logical_batch_id=(0, 1),
        key_id=transfer_slice.index,
        batch_index=1,
        rows=10,
        timing=staged_keyed_pipeline.BatchTiming(1.0, 1.2, 1.3, 2.0, 1024),
        writer_id=0,
    )
    assert first is not None
    staged_keyed_logging.log_batch_progress(task, first)
    now[0] = 4.0
    second = tracker.commit_batch(
        logical_batch_id=(0, 2),
        key_id=transfer_slice.index,
        batch_index=2,
        rows=10,
        timing=staged_keyed_pipeline.BatchTiming(3.0, 3.2, 3.3, 4.0, 2048),
        writer_id=0,
    )
    assert second is not None
    staged_keyed_logging.log_batch_progress(task, second)
    verification = tracker.verify_key(transfer_slice.index)
    assert verification is not None
    staged_keyed_logging.log_key_verification(task, verification)
    other_slice = (options.transfer_slices or [])[1]
    tracker.start_key(other_slice.index)
    tracker.materialize_key(other_slice.index, 0)
    tracker.assign_key(other_slice.index, 0)
    assert tracker.verify_key(other_slice.index) is not None
    loading = tracker.mark_loading_complete()
    staged_keyed_logging.log_loading_complete(loading)

    assert messages[0].startswith("Starting keyed source-stage transfer: 2 keys")
    assert "source connection limit 1; target connection limit 1" in messages[0]
    assert messages[1].startswith(f"{task.tag} Staged batch 1: 10 rows")
    assert "rolling rate unavailable" in messages[1]
    assert "load ETA unavailable; total transfer ETA unavailable" in messages[1]
    assert messages[2].startswith(f"{task.tag} Staged batch 2: 10 rows")
    assert "rolling rate 7 rows/s" in messages[2]
    assert "approximate RAM rate" in messages[2]
    assert messages[3].startswith(f"{task.tag} Verified 20 rows")
    assert messages[4].startswith("Completed source-stage loading: 20 rows")
