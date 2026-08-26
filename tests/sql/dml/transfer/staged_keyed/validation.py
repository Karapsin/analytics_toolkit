from __future__ import annotations

from tests.sql._support.staged_keyed import (
    Any,
    BoundedConnectionManager,
    KeyReadComplete,
    LazyKeyedRuntime,
    QueuedKeyBatch,
    ReadyKeyTask,
    RowBatch,
    TransferProgressTracker,
    VerifiedKey,
    _concurrency,
    _LeaseManager,
    _metadata,
    _options,
    _ready_task,
    _state,
    _thread,
    pytest,
    staged_keyed_pipeline,
    threading,
)


def test_bounded_connection_manager_rejects_open_completed_after_interrupt() -> None:
    open_started = threading.Event()
    release_open = threading.Event()
    yielded = threading.Event()

    class Connection:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    opened: list[Connection] = []

    def open_connection(_key: str) -> Connection:
        open_started.set()
        assert release_open.wait(2)
        connection = Connection()
        opened.append(connection)
        return connection

    manager = BoundedConnectionManager(
        "source",
        1,
        role="source interrupt race pool",
        open_connection=open_connection,
    )

    def lease() -> None:
        with manager.lease():
            yielded.set()

    worker, errors = _thread(lease)
    assert open_started.wait(2)
    manager.interrupt_active()
    release_open.set()
    worker.join(2)

    assert not worker.is_alive()
    assert not yielded.is_set()
    assert len(errors) == 1
    assert "opening was cancelled" in str(errors[0])
    assert [connection.close_count for connection in opened] == [1]
    manager.close()


def test_bounded_connection_manager_rejects_replacement_completed_after_interrupt() -> None:
    replacement_started = threading.Event()
    release_replacement = threading.Event()
    replacement_returned = threading.Event()

    class Connection:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    opened: list[Connection] = []

    def open_connection(_key: str) -> Connection:
        connection = Connection()
        opened.append(connection)
        if len(opened) == 2:
            replacement_started.set()
            assert release_replacement.wait(2)
        return connection

    manager = BoundedConnectionManager(
        "source",
        1,
        role="source replacement race pool",
        open_connection=open_connection,
    )

    def replace() -> None:
        with manager.lease() as ref:
            manager.replace_connection("source", ref)
            replacement_returned.set()

    worker, errors = _thread(replace)
    assert replacement_started.wait(2)
    manager.interrupt_active()
    release_replacement.set()
    worker.join(2)

    assert not worker.is_alive()
    assert not replacement_returned.is_set()
    assert len(errors) == 1
    assert "replacement" in str(errors[0])
    assert [connection.close_count for connection in opened] == [1, 1]
    manager.close()


def test_key_acknowledgement_is_published_only_after_target_validation(
    monkeypatch: Any,
) -> None:
    options = _options(
        transfer_slices=[_options().transfer_slices[0]],  # type: ignore[index]
        transfer_concurrency=_concurrency(1, 1),
    )
    transfer_slice = (options.transfer_slices or [])[0]

    def setup() -> tuple[
        LazyKeyedRuntime,
        ReadyKeyTask,
        TransferProgressTracker,
    ]:
        runtime = LazyKeyedRuntime([transfer_slice], read_workers=1, write_workers=1)
        task = _ready_task(transfer_slice, "source.zero", 0)
        task.writer_index = 0
        task.batch_queue = runtime.writer_queues[0]
        task.batch_queue.put_nowait(KeyReadComplete(task, streamed_rows=0, batch_count=0))
        progress = TransferProgressTracker(total_key_count=1, active_writers=1)
        progress.start_key(transfer_slice.index, started_at=0.0)
        progress.materialize_key(transfer_slice.index, 0, started_at=0.0)
        progress.assign_key(transfer_slice.index, 0)
        return runtime, task, progress

    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        staged_keyed_pipeline, "log_key_verification", lambda *_args, **_kwargs: None
    )
    failed_runtime, failed_task, failed_progress = setup()
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_target_key",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("invalid target key")),
    )

    with pytest.raises(RuntimeError, match="invalid target key"):
        staged_keyed_pipeline._consume_key(
            options,
            _metadata(),
            _state(),
            failed_runtime,
            _LeaseManager(),  # type: ignore[arg-type]
            failed_progress,
            threading.Lock(),
            0,
            None,
            failed_task,
            staged_keyed_pipeline._make_batch_sizer(options),
            1,
        )
    assert failed_runtime.verified == {}
    assert failed_runtime.drop_ready.empty()

    runtime, task, progress = setup()
    events: list[str] = []
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_target_key",
        lambda *_args: events.append("validated"),
    )
    original_mark_verified = runtime.mark_verified

    def mark_verified(checkpoint: Any) -> None:
        events.append("checkpointed")
        original_mark_verified(checkpoint)

    monkeypatch.setattr(runtime, "mark_verified", mark_verified)
    staged_keyed_pipeline._consume_key(
        options,
        _metadata(),
        _state(),
        runtime,
        _LeaseManager(),  # type: ignore[arg-type]
        progress,
        threading.Lock(),
        0,
        None,
        task,
        staged_keyed_pipeline._make_batch_sizer(options),
        1,
    )

    acknowledgement = runtime.drop_ready.get_nowait()
    assert events == ["validated", "checkpointed"]
    assert acknowledgement.task is task
    assert runtime.verified[transfer_slice.index].expected_rows == 0


@pytest.mark.parametrize(
    ("malformation", "message"),
    [
        ("task", "Source batch belongs to a different key"),
        ("batch_order", "Logical source batch order is not contiguous"),
    ],
)
def test_key_consumer_rejects_malformed_queued_batch(
    malformation: str,
    message: str,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(slices, read_workers=1, write_workers=1)
    task = _ready_task(slices[0], "source.key", 1)
    other_task = _ready_task(slices[1], "source.other", 1)
    task.batch_queue = runtime.writer_queues[0]
    task.batch_queue.put_nowait(
        QueuedKeyBatch(
            task=other_task if malformation == "task" else task,
            batch_index=2 if malformation == "batch_order" else 1,
            start_ordinal=1,
            stop_ordinal=2,
            batch=RowBatch(columns=["id"], rows=[(1,)]),
            read_started_at=0.0,
            read_completed_at=0.1,
            approximate_memory_bytes=8,
        )
    )

    with pytest.raises(RuntimeError, match=message):
        staged_keyed_pipeline._consume_key(
            options,
            _metadata(),
            _state(),
            runtime,
            _LeaseManager(),  # type: ignore[arg-type]
            TransferProgressTracker(total_key_count=2, active_writers=1),
            threading.Lock(),
            0,
            "target.stage",
            task,
            staged_keyed_pipeline._make_batch_sizer(options),
            1,
        )


@pytest.mark.parametrize(
    ("malformation", "message"),
    [
        ("task", "Source batch completion marker is inconsistent"),
        ("batch_count", "Source batch completion marker is inconsistent"),
        ("streamed_rows", "Source and writer batch totals do not match"),
    ],
)
def test_key_consumer_rejects_malformed_read_completion(
    malformation: str,
    message: str,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(slices, read_workers=1, write_workers=1)
    task = _ready_task(slices[0], "source.key", 0)
    other_task = _ready_task(slices[1], "source.other", 0)
    task.batch_queue = runtime.writer_queues[0]
    completion = KeyReadComplete(
        other_task if malformation == "task" else task,
        streamed_rows=1 if malformation == "streamed_rows" else 0,
        batch_count=1 if malformation == "batch_count" else 0,
    )
    task.batch_queue.put_nowait(completion)

    with pytest.raises(RuntimeError, match=message):
        staged_keyed_pipeline._consume_key(
            options,
            _metadata(),
            _state(),
            runtime,
            _LeaseManager(),  # type: ignore[arg-type]
            TransferProgressTracker(total_key_count=2, active_writers=1),
            threading.Lock(),
            0,
            None,
            task,
            staged_keyed_pipeline._make_batch_sizer(options),
            1,
        )


def test_keyed_reader_rejects_a_short_requested_source_range(monkeypatch: Any) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1), batch_size=2)
    transfer_slice = (options.transfer_slices or [])[0]
    runtime = LazyKeyedRuntime([transfer_slice], read_workers=1, write_workers=1)
    task = _ready_task(transfer_slice, "source.key", 2)
    task.batch_size = 2
    task.batch_queue = runtime.writer_queues[0]
    task.batch_slot = runtime.writer_batch_slots[0]
    source_connections = _LeaseManager()
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "read_key_batch",
        lambda *_args, **_kwargs: RowBatch(columns=["id"], rows=[(1,)]),
    )

    with pytest.raises(RuntimeError, match=r"returned 1 row\(s\); expected 2"):
        staged_keyed_pipeline._stream_ready_key(
            options,
            _metadata(),
            task,
            runtime,
            source_connections,  # type: ignore[arg-type]
        )

    assert task.batch_queue.empty()
    assert source_connections.active == 0


def test_keyed_reader_rejects_an_overlong_requested_source_range(monkeypatch: Any) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1), batch_size=2)
    transfer_slice = (options.transfer_slices or [])[0]
    runtime = LazyKeyedRuntime([transfer_slice], read_workers=1, write_workers=1)
    task = _ready_task(transfer_slice, "source.key", 2)
    task.batch_size = 2
    task.batch_queue = runtime.writer_queues[0]
    task.batch_slot = runtime.writer_batch_slots[0]
    source_connections = _LeaseManager()
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "read_key_batch",
        lambda *_args, **_kwargs: RowBatch(columns=["id"], rows=[(1,), (2,), (3,)]),
    )

    with pytest.raises(RuntimeError, match=r"returned 3 row\(s\); expected 2"):
        staged_keyed_pipeline._stream_ready_key(
            options,
            _metadata(),
            task,
            runtime,
            source_connections,  # type: ignore[arg-type]
        )

    assert task.batch_queue.empty()
    assert task.batch_slot.acquire(blocking=False) is True
    assert source_connections.active == 0


def test_runtime_rejects_duplicate_logical_batch_commit() -> None:
    runtime = LazyKeyedRuntime([], read_workers=1, write_workers=1)
    logical_id = (0, 1, 1, 3)
    runtime.mark_batch_success(logical_id)
    with pytest.raises(RuntimeError, match="committed twice"):
        runtime.mark_batch_success(logical_id)


def test_stage_state_sync_requires_every_key_to_be_verified() -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(slices, read_workers=1, write_workers=1)
    runtime.mark_verified(
        VerifiedKey(
            slice_index=slices[0].index,
            expected_rows=1,
            streamed_rows=1,
            target_stage="target.stage_0",
        )
    )

    with pytest.raises(RuntimeError, match="Not every transfer key reached"):
        staged_keyed_pipeline._sync_stage_state(
            options,
            _state(),
            runtime,
            require_complete=True,
        )


def test_target_stage_validation_checks_payload_counts_without_internal_columns(
    monkeypatch: Any,
) -> None:
    options = _options()
    state = _state()
    validated: list[dict[int, int]] = []
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_transfer_stage_identity",
        lambda **kwargs: validated.append(dict(kwargs["expected_slice_counts"])),
    )

    staged_keyed_pipeline._validate_target_stages(
        options,
        {"connection": object()},
        state,
        ["target"],
        {0: 1},
        {0: 1},
    )
    assert validated == [{0: 1}]

    with pytest.raises(RuntimeError, match="row-count mismatch"):
        staged_keyed_pipeline._validate_target_stages(
            options,
            {"connection": object()},
            state,
            ["target"],
            {0: 1},
            {0: 0},
        )
    with pytest.raises(RuntimeError, match="no target stage"):
        staged_keyed_pipeline._validate_target_stages(
            options,
            {"connection": object()},
            state,
            [],
            {0: 1},
            {0: 1},
        )
    staged_keyed_pipeline._validate_target_stages(
        options,
        {"connection": object()},
        state,
        [],
        {0: 0},
        {0: 0},
    )
    state.internal_columns = None
    staged_keyed_pipeline._validate_target_stages(
        options,
        {"connection": object()},
        state,
        ["target"],
        {0: 1},
        {0: 1},
    )
    assert validated == [{0: 1}, {0: 1}]
