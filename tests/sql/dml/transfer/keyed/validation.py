from __future__ import annotations

from tests.sql._support.transfer_keyed import (
    Any,
    KeyReadComplete,
    LazyKeyedRuntime,
    RowBatch,
    SimpleNamespace,
    TransferSlice,
    TransferStageState,
    _consume,
    _ConsumeCase,
    _ConsumeProgress,
    _Manager,
    _metadata,
    _options,
    _queued,
    _task,
    pytest,
    staged_keyed_io,
    staged_keyed_logging,
    staged_keyed_pipeline,
    staged_keyed_stream,
)


def test_consumer_rejects_duplicate_key_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options()
    runtime = LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=1)
    task = _task(options, expected_rows=0)
    task.batch_queue = runtime.writer_queues[0]
    task.batch_queue.put_nowait(KeyReadComplete(task, streamed_rows=0, batch_count=0))

    with pytest.raises(RuntimeError, match="Target key was verified twice"):
        _consume(
            monkeypatch,
            options,
            runtime,
            task,
            _ConsumeCase(None, _ConsumeProgress(verification=None)),
        )


def test_consumer_rejects_insert_count_and_duplicate_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options()
    runtime = LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=1)
    task = _task(options)
    task.batch_queue = runtime.writer_queues[0]
    task.batch_queue.put_nowait(_queued(task))
    with pytest.raises(RuntimeError, match="committed 0 row"):
        _consume(
            monkeypatch,
            options,
            runtime,
            task,
            _ConsumeCase("target.stage", _ConsumeProgress(), inserted_rows=0),
        )

    runtime = LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=1)
    task = _task(options)
    task.batch_queue = runtime.writer_queues[0]
    task.batch_queue.put_nowait(_queued(task))
    with pytest.raises(RuntimeError, match="acknowledged twice"):
        _consume(
            monkeypatch,
            options,
            runtime,
            task,
            _ConsumeCase("target.stage", _ConsumeProgress(commit_result=None)),
        )


def test_consumer_rejects_missing_queue_and_batch_without_target_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options()
    runtime = LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=1)
    task = _task(options)
    with pytest.raises(RuntimeError, match="queue assignment"):
        _consume(
            monkeypatch,
            options,
            runtime,
            task,
            _ConsumeCase("target.stage", _ConsumeProgress()),
        )

    task.batch_queue = runtime.writer_queues[0]
    task.batch_queue.put_nowait(_queued(task))
    with pytest.raises(RuntimeError, match="no target writer stage"):
        _consume(
            monkeypatch,
            options,
            runtime,
            task,
            _ConsumeCase(None, _ConsumeProgress()),
        )


def test_keyed_io_count_and_validation_forward_cached_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options()
    metadata = _metadata()
    task = _task(options, expected_rows=7)
    queries: list[str] = []
    validations: list[dict[str, Any]] = []
    adapter = SimpleNamespace(quote_identifier=lambda name: f'"{name}"')
    monkeypatch.setattr(staged_keyed_io, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(
        staged_keyed_io,
        "_read_backend",
        lambda _backend, _connection, sql, **_kwargs: (
            queries.append(sql) or SimpleNamespace(columns=[[7]])
        ),
    )
    monkeypatch.setattr(
        staged_keyed_io,
        "validate_transfer_stage_slice",
        lambda **kwargs: validations.append(kwargs),
    )

    assert staged_keyed_io.count_source_slice(options, object(), "source.stage", 0, metadata) == 7
    staged_keyed_io.validate_target_key(
        options,
        {"connection": "target-connection"},
        metadata,
        task,
        "target.stage",
        7,
    )

    assert queries == ['SELECT COUNT(*) FROM source.stage WHERE "__analytics_toolkit_slice_id" = 0']
    assert validations[0]["connection"] == "target-connection"
    assert validations[0]["expected_count"] == 7
    assert validations[0]["streamed_count"] == 7


def test_keyed_io_rejects_unequal_and_normalized_oversized_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(retry_cnt=1)
    task = _task(options, expected_rows=2)
    metadata = _metadata()
    result = SimpleNamespace(column_names=["id", "other"], columns=[[1, 2], [3]])
    monkeypatch.setattr(staged_keyed_io, "_read_backend", lambda *_args, **_kwargs: result)

    with pytest.raises(RuntimeError, match="unequal lengths"):
        staged_keyed_io.read_key_batch(
            options,
            {"connection": object()},
            task,
            metadata,
            1,
            3,
        )

    result = SimpleNamespace(column_names=["id"], columns=[[1, 2]])
    adapter = SimpleNamespace(
        normalize_transfer_source_batch=lambda _batch, _types: RowBatch(
            ["id"],
            [(1,), (2,), (3,)],
        )
    )
    monkeypatch.setattr(staged_keyed_io, "_read_backend", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(staged_keyed_io, "get_backend_adapter", lambda _backend: adapter)

    with pytest.raises(RuntimeError, match="Normalized source batch returned 3"):
        staged_keyed_io.read_key_batch(
            options,
            {"connection": object()},
            task,
            metadata,
            1,
            3,
        )


def test_keyed_logging_invalid_slice_zero_nonzero_and_summary_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options()
    messages: list[str] = []
    monkeypatch.setattr(staged_keyed_logging, "time_print", messages.append)

    invalid = TransferSlice(9, (9,), "", "SELECT 9", "key=9")
    with pytest.raises(ValueError, match="outside the normalized"):
        staged_keyed_logging.slice_tag(options, invalid)

    zero = SimpleNamespace(committed_rows=0)
    runtime = LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=1)
    staged_keyed_logging.log_transfer_complete(options, zero, runtime, 0.5)
    assert "no batch throughput" in messages[-1]

    runtime.register_target_stage(0, "target.stage")
    runtime.source_stages_dropped = 2
    nonzero = SimpleNamespace(
        committed_rows=3,
        average_rows_per_second=2.0,
        average_memory_bytes_per_second=1024.0,
    )
    staged_keyed_logging.log_transfer_complete(options, nonzero, runtime, 1.5)
    assert "target stages cleaned 1/1" in messages[-1]

    class NoSummaryError(RuntimeError):
        @property
        def __dict__(self) -> dict[str, Any]:
            message = "summary storage disabled"
            raise AttributeError(message)

    staged_keyed_logging.attach_attempt_summary(
        NoSummaryError("failure"),
        SimpleNamespace(committed_rows=3, attempt_elapsed_seconds=1.5),
        "loading",
    )


def test_lazy_workers_cancel_on_sentinel_failure_and_reject_residual_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(transfer_slices=[])
    state = TransferStageState(target_exists=True)
    runtime = LazyKeyedRuntime([], read_workers=1, write_workers=1)
    failure = OSError("sentinel handoff failed")
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_put_with_cancellation",
        lambda *_args: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(OSError, match="sentinel handoff failed") as exc_info:
        staged_keyed_pipeline._run_lazy_workers(
            options,
            _metadata(),
            state,
            runtime,
            _Manager(),
            _Manager(),
            SimpleNamespace(),
            insert_retry_cnt=1,
        )
    assert exc_info.value is failure
    assert runtime.cancellation.is_set()

    monkeypatch.undo()
    runtime = LazyKeyedRuntime([], read_workers=1, write_workers=1)
    assert runtime.live_stage_credits.acquire(blocking=False)
    runtime.reserve_source_stage("source.leftover")
    with pytest.raises(RuntimeError, match="mandatory final drain"):
        staged_keyed_pipeline._run_lazy_workers(
            options,
            _metadata(),
            state,
            runtime,
            _Manager(),
            _Manager(),
            SimpleNamespace(),
            insert_retry_cnt=1,
        )


def test_stream_requires_writer_queue_assignment() -> None:
    options = _options()
    runtime = LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=1)
    task = _task(options)
    context = staged_keyed_stream.KeyStreamContext(
        options=options,
        metadata=_metadata(),
        runtime=runtime,
        source_connections=_Manager(),
    )
    callbacks = staged_keyed_stream.KeyStreamCallbacks(
        drain_drop_ready=lambda *_args, **_kwargs: 0,
    )

    with pytest.raises(RuntimeError, match="capacity-one batch queue"):
        staged_keyed_stream.stream_ready_key(context, task, callbacks)
