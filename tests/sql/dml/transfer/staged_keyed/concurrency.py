from __future__ import annotations

from tests.sql._support.staged_keyed import (
    Any,
    Callable,
    KeyReadComplete,
    LazyKeyedRuntime,
    QueuedKeyBatch,
    ReadyKeyTask,
    RowBatch,
    TransferProgressTracker,
    _concurrency,
    _LeaseManager,
    _metadata,
    _options,
    _ready_task,
    _state,
    _thread,
    staged_keyed_pipeline,
    threading,
    time,
)


def test_concurrent_keyed_batch_logs_follow_monotonic_commit_order(
    monkeypatch: Any,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 2))
    slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(slices, read_workers=1, write_workers=2)
    progress = TransferProgressTracker(total_key_count=2, active_writers=2)
    state = _state()
    state_lock = threading.Lock()
    tasks: list[ReadyKeyTask] = []
    now = time.monotonic()
    for writer_index, transfer_slice in enumerate(slices):
        task = _ready_task(transfer_slice, f"source.key_{writer_index}", 1)
        task.writer_index = writer_index
        task.batch_queue = runtime.writer_queues[writer_index]
        batch = RowBatch(columns=["id"], rows=[(writer_index + 1,)])
        task.batch_queue.put_nowait(
            QueuedKeyBatch(
                task=task,
                batch_index=1,
                start_ordinal=1,
                stop_ordinal=2,
                batch=batch,
                read_started_at=now,
                read_completed_at=now,
                queued_at=now,
                approximate_memory_bytes=batch.approx_memory_bytes(),
            )
        )
        progress.start_key(transfer_slice.index, started_at=now)
        progress.materialize_key(transfer_slice.index, 1, started_at=now)
        progress.assign_key(transfer_slice.index, writer_index)
        tasks.append(task)

    first_log_started = threading.Event()
    release_first_log = threading.Event()
    second_log_started = threading.Event()
    committed_totals: list[int] = []

    def log_batch(_task: ReadyKeyTask, batch_progress: Any) -> None:
        if batch_progress.key_id == slices[0].index:
            first_log_started.set()
            assert release_first_log.wait(2)
        else:
            second_log_started.set()
        committed_totals.append(batch_progress.snapshot.committed_rows)

    monkeypatch.setattr(
        staged_keyed_pipeline,
        "insert_target_batch",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(staged_keyed_pipeline, "validate_target_key", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_batch_progress", log_batch)
    monkeypatch.setattr(
        staged_keyed_pipeline, "log_key_verification", lambda *_args, **_kwargs: None
    )

    def consume(writer_index: int) -> None:
        staged_keyed_pipeline._consume_key(
            options,
            _metadata(),
            state,
            runtime,
            _LeaseManager(),  # type: ignore[arg-type]
            progress,
            state_lock,
            writer_index,
            f"target.stage_{writer_index}",
            tasks[writer_index],
            staged_keyed_pipeline._make_batch_sizer(options),
            1,
        )

    first, first_errors = _thread(lambda: consume(0))
    assert first_log_started.wait(2)
    tasks[0].batch_queue.put(
        KeyReadComplete(tasks[0], streamed_rows=1, batch_count=1),
        timeout=1,
    )
    second, second_errors = _thread(lambda: consume(1))
    tasks[1].batch_queue.put(
        KeyReadComplete(tasks[1], streamed_rows=1, batch_count=1),
        timeout=1,
    )
    assert not second_log_started.wait(0.1)
    release_first_log.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert first_errors == []
    assert second_errors == []
    assert committed_totals == [1, 2]


def test_concurrent_writers_keep_whole_key_ownership(monkeypatch: Any) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 2))
    slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(slices, read_workers=1, write_workers=2)
    tasks = [
        _ready_task(slices[0], "source.key_0", 0),
        _ready_task(slices[1], "source.key_1", 0),
    ]
    progress = TransferProgressTracker(total_key_count=2, active_writers=2)
    barrier = threading.Barrier(2)
    ownership: list[tuple[int, int]] = []
    ownership_lock = threading.Lock()

    def consume(*args: Any, **_kwargs: Any) -> None:
        writer_index = int(args[7])
        task = args[9]
        with ownership_lock:
            ownership.append((task.transfer_slice.index, writer_index))
        barrier.wait(timeout=1)

    monkeypatch.setattr(staged_keyed_pipeline, "_consume_key", consume)
    runtime.ready.put(tasks[0])
    runtime.ready.put(tasks[1])

    def writer_operation(writer_index: int) -> Callable[[], None]:
        def run() -> None:
            staged_keyed_pipeline._writer_worker(
                options,
                _metadata(),
                _state(),
                runtime,
                _LeaseManager(),  # type: ignore[arg-type]
                progress,
                threading.Lock(),
                writer_index,
                1,
            )

        return run

    workers = [_thread(writer_operation(writer_index)) for writer_index in range(2)]
    assert all(task.assignment.wait(timeout=1) for task in tasks)
    runtime.ready.put(None, timeout=1)
    runtime.ready.put(None, timeout=1)
    for worker, _errors in workers:
        worker.join(timeout=2)

    assert all(not worker.is_alive() for worker, _errors in workers)
    assert all(errors == [] for _worker, errors in workers)
    assert {slice_index for slice_index, _writer in ownership} == {0, 1}
    assert len(ownership) == 2
    assert {writer for _slice_index, writer in ownership} == {0, 1}
    for task in tasks:
        assert task.writer_index is not None
        assert task.batch_queue is runtime.writer_queues[task.writer_index]
