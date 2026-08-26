from __future__ import annotations

from tests.sql._support.staged_keyed import (
    Any,
    BoundedConnectionCloseError,
    BoundedConnectionManager,
    LazyKeyedRuntime,
    QueuedKeyBatch,
    ReadyKeyTask,
    RowBatch,
    SimpleNamespace,
    TransferConnectionRefs,
    TransferOptions,
    TransferSlice,
    TransferStageState,
    VerifiedKey,
    _concurrency,
    _LeaseManager,
    _metadata,
    _options,
    _ProgressBar,
    _ready_task,
    _state,
    _thread,
    finalize,
    pytest,
    staged_keyed_io,
    staged_keyed_pipeline,
    threading,
    transfer_api,
    transfer_retry,
)


def test_bounded_connection_manager_close_failure_is_nonretryable_and_tracked() -> None:
    opened = 0

    class Connection:
        def close(self) -> None:
            raise OSError("still live")

    def open_connection(_key: str) -> Connection:
        nonlocal opened
        opened += 1
        return Connection()

    manager = BoundedConnectionManager(
        "source",
        1,
        role="source strict close pool",
        open_connection=open_connection,
    )
    with manager.lease():
        pass
    with pytest.raises(BoundedConnectionCloseError) as exc_info:
        manager.close()

    assert exc_info.value.analytics_toolkit_sql_retry_safe is False
    assert opened == 1
    with pytest.raises(RuntimeError, match="manager is closed"), manager.lease():
        pass


def test_bounded_connection_manager_never_opens_after_close_failure() -> None:
    opened = 0

    class Connection:
        def close(self) -> None:
            raise OSError("close failed")

    def open_connection(_key: str) -> Connection:
        nonlocal opened
        opened += 1
        return Connection()

    manager = BoundedConnectionManager(
        "source",
        1,
        role="source test pool",
        open_connection=open_connection,
    )
    with manager.lease() as ref, pytest.raises(
        RuntimeError,
        match="no replacement was opened",
    ):
        manager.replace_connection("source", ref)
    assert opened == 1


def test_cancelled_opener_close_failure_remains_tracked_for_strict_retry() -> None:
    open_started = threading.Event()
    release_open = threading.Event()

    class Connection:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1
            if self.close_count == 1:
                raise OSError("first close failed")

    connection = Connection()

    def open_connection(_key: str) -> Connection:
        open_started.set()
        assert release_open.wait(2)
        return connection

    manager = BoundedConnectionManager(
        "source",
        1,
        role="source rejected-open pool",
        open_connection=open_connection,
    )
    worker, errors = _thread(lambda: manager.run("open", lambda _ref: None))
    assert open_started.wait(2)
    manager.interrupt_active()
    release_open.set()
    worker.join(2)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], BoundedConnectionCloseError)
    manager.close()
    assert connection.close_count == 2


def test_failed_attempt_cleanup_drops_published_zero_row_source_stages(
    monkeypatch: Any,
) -> None:
    options = _options()
    transfer_slices = options.transfer_slices or []
    runtime = LazyKeyedRuntime(transfer_slices, read_workers=2, write_workers=2)
    zero_task = _ready_task(transfer_slices[0], "source.zero", 0)
    nonempty_task = _ready_task(transfer_slices[1], "source.nonempty", 1)
    for stage_table in ("source.reserved", zero_task.source_stage, nonempty_task.source_stage):
        assert runtime.live_stage_credits.acquire(blocking=False)
        runtime.reserve_source_stage(stage_table)
    runtime.publish_source_stage(zero_task)
    runtime.publish_source_stage(nonempty_task)
    dropped: list[str] = []
    monkeypatch.setattr(
        staged_keyed_io,
        "cleanup_source_stages",
        lambda _options, _source_ref, stage_tables: dropped.extend(stage_tables),
    )

    staged_keyed_io.cleanup_failed_empty_source_stages(
        options,
        runtime,
        _LeaseManager(),  # type: ignore[arg-type]
    )

    assert dropped == ["source.reserved", "source.zero"]
    assert runtime.source_stage_tables == ["source.nonempty"]
    assert runtime.source_stages_dropped == 2


def test_keyed_io_opts_all_batch_retries_into_safe_exception_logging(
    monkeypatch: Any,
) -> None:
    options = _options(retry_cnt=1)
    task = _ready_task(options.transfer_slices[0], "source.stage", 1)
    metadata = _metadata()
    retry_calls: list[dict[str, Any]] = []

    def retry_once(**kwargs: Any) -> Any:
        retry_calls.append(kwargs)
        return kwargs["operation"](1)

    monkeypatch.setattr(staged_keyed_io, "run_with_retry", retry_once)
    monkeypatch.setattr(
        staged_keyed_io,
        "_read_backend",
        lambda *_args, **_kwargs: SimpleNamespace(
            column_names=["id"],
            columns=[[1]],
        ),
    )
    monkeypatch.setattr(staged_keyed_io, "cleanup_stage_table", lambda *_a, **_k: None)

    def insert_once(*_args: Any, **kwargs: Any) -> int:
        assert kwargs["safe_exception_logging"] is True
        assert kwargs["log_prefix"] == f"{task.tag} "
        return kwargs["retry_fn"](
            operation_name="keyed insert",
            retry_cnt=1,
            timeout_increment=0,
            operation=lambda _attempt: 1,
        )

    monkeypatch.setattr(staged_keyed_io, "insert_rows_batch", insert_once)

    staged_keyed_io.read_key_batch(
        options,
        {"connection": object()},
        task,
        metadata,
        1,
        2,
    )
    staged_keyed_io.insert_target_batch(
        options,
        {"connection": object()},
        "target.stage",
        QueuedKeyBatch(
            task=task,
            batch_index=1,
            start_ordinal=1,
            stop_ordinal=1,
            batch=RowBatch(columns=["id"], rows=[(1,)]),
            read_started_at=0.0,
            read_completed_at=0.1,
            approximate_memory_bytes=8,
        ),
        metadata,
        insert_retry_cnt=1,
    )
    staged_keyed_io.drop_source_stage(options, {"connection": object()}, task)

    assert len(retry_calls) == 3
    assert all(call["safe_exception_logging"] is True for call in retry_calls)
    assert all(call["log_prefix"] == f"{task.tag} " for call in retry_calls)
    assert all(callable(call["retry_status"]) for call in retry_calls)


def test_keyed_target_cleanup_retry_redacts_exception_details(monkeypatch: Any) -> None:
    messages: list[str] = []
    state = _state()
    state.stage_table = "target_stage.writer_0"
    state.stage_table_created = True

    def cleanup_with_retry(*_args: Any, **kwargs: Any) -> None:
        def fail(_attempt: int) -> None:
            raise RuntimeError("secret row and SQL text")

        kwargs["retry_fn"](
            operation_name="keyed cleanup",
            retry_cnt=1,
            timeout_increment=0,
            operation=fail,
        )

    monkeypatch.setattr(finalize, "cleanup_stage_table_with_retry", cleanup_with_retry)
    monkeypatch.setattr(
        transfer_retry,
        "time_print",
        lambda message, **_kwargs: messages.append(message),
    )

    with pytest.raises(RuntimeError, match="secret row"):
        finalize.cleanup_stage(
            _options(),
            TransferConnectionRefs(),
            state,
            1,
            target_connection_runner=lambda _role, operation: operation({"connection": object()}),
            safe_exception_logging=True,
        )

    assert messages
    assert all("secret row" not in message for message in messages)
    assert any("RuntimeError" in message for message in messages)


def test_persistent_acknowledged_drop_failure_blocks_finalization(monkeypatch: Any) -> None:
    transfer_slice = TransferSlice(0, (1,), "", "SELECT 1 AS id", "key=1")
    options = _options(
        transfer_slices=[transfer_slice],
        transfer_concurrency=_concurrency(1, 1),
    )
    metadata = _metadata()
    state = _state()
    finalization_calls: list[str] = []

    class AttemptConnectionManager(_LeaseManager):
        def __init__(
            self,
            _connection_key: str,
            _capacity: int,
            *,
            role: str,
            **_kwargs: Any,
        ) -> None:
            super().__init__()
            self.role = role

        def close(self) -> None:
            return

    monkeypatch.setattr(
        staged_keyed_pipeline,
        "BoundedConnectionManager",
        AttemptConnectionManager,
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "make_transfer_progress_bar",
        lambda *_args, **_kwargs: _ProgressBar(),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "create_stage_state", lambda *_args: state)
    monkeypatch.setattr(staged_keyed_pipeline, "_prepare_attempt", lambda *_args: metadata)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "allocate_source_stage_name",
        lambda *_args: "source.exact",
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "materialize_source_key",
        lambda *_args: 0,
    )
    monkeypatch.setattr(staged_keyed_pipeline, "validate_target_key", lambda *_args: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "drop_source_stage",
        lambda *_args: (_ for _ in ()).throw(OSError("persistent drop failure")),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "cleanup_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_pipeline_start", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_key_verification", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_validate_target_stages",
        lambda *_args: finalization_calls.append("aggregate validation"),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_loaded_stage_row_count",
        lambda **_kwargs: finalization_calls.append("row-count validation"),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "finalize_loaded_stage",
        lambda *_args: finalization_calls.append("destination mutation"),
    )

    with pytest.raises(
        staged_keyed_pipeline.AcknowledgedSourceStageDropError,
        match="Could not drop an acknowledged source stage",
    ):
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=1,
        )

    assert finalization_calls == []
    assert state.source_stage_tables == ["source.exact"]


def test_public_keyed_full_retry_rematerializes_dropped_key_with_fresh_runtime(  # noqa: PLR0915
    monkeypatch: Any,
) -> None:
    options = _options(
        transfer_concurrency=_concurrency(1, 1),
        replace_target_table=True,
        write_mode="replace",
        retry_cnt=1,
        timeout_increment=0,
        full_retry_cnt=2,
        full_timeout_increment=0,
    )
    created_runtimes: list[LazyKeyedRuntime] = []
    initial_checkpoints: list[dict[int, VerifiedKey]] = []
    attempt_states: list[TransferStageState] = []
    metadata_attempts: list[int] = []
    transfer_ids: list[str | None] = []
    materialized: dict[int, list[int]] = {}
    validated: dict[int, list[int]] = {}
    dropped: list[tuple[int, int]] = []
    finalized: list[int] = []
    first_key_dropped = threading.Event()

    class RecordingRuntime(LazyKeyedRuntime):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created_runtimes.append(self)
            initial_checkpoints.append(self.verified)

    class AttemptConnectionManager(_LeaseManager):
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            super().__init__()

    def create_state(*_args: Any) -> TransferStageState:
        state = _state()
        attempt_states.append(state)
        return state

    def prepare(
        current_options: TransferOptions,
        _refs: TransferConnectionRefs,
        _stage_state: TransferStageState,
    ) -> Any:
        metadata_attempts.append(current_options.attempt_number)
        transfer_ids.append(current_options.transfer_id)
        return _metadata()

    def materialize(
        current_options: TransferOptions,
        _source_ref: Any,
        _metadata_value: Any,
        transfer_slice: TransferSlice,
        _source_stage: str,
    ) -> int:
        materialized.setdefault(current_options.attempt_number, []).append(transfer_slice.index)
        return 1

    def validate(
        current_options: TransferOptions,
        _target_ref: Any,
        _metadata_value: Any,
        task: ReadyKeyTask,
        _target_stage: Any,
        streamed_rows: int,
    ) -> None:
        attempt = current_options.attempt_number
        validated.setdefault(attempt, []).append(task.transfer_slice.index)
        assert streamed_rows == 1
        if attempt == 1 and task.transfer_slice.index == 1:
            assert first_key_dropped.wait(timeout=1)
            raise OSError("second key failed after the first source stage was dropped")

    def drop(
        current_options: TransferOptions,
        _source_ref: Any,
        task: ReadyKeyTask,
    ) -> None:
        dropped.append((current_options.attempt_number, task.transfer_slice.index))
        if current_options.attempt_number == 1 and task.transfer_slice.index == 0:
            first_key_dropped.set()

    monkeypatch.setattr(transfer_api, "build_transfer_options", lambda **_kwargs: options)
    monkeypatch.setattr(staged_keyed_pipeline, "LazyKeyedRuntime", RecordingRuntime)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "BoundedConnectionManager",
        AttemptConnectionManager,
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "make_transfer_progress_bar",
        lambda *_args, **_kwargs: _ProgressBar(),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "create_stage_state", create_state)
    monkeypatch.setattr(staged_keyed_pipeline, "_prepare_attempt", prepare)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "allocate_source_stage_name",
        lambda current_options, _ref, slice_index: (
            f"source.attempt_{current_options.attempt_number}.slice_{slice_index}"
        ),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "materialize_source_key", materialize)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "create_target_writer_stage",
        lambda current_options, _ref, _metadata_value, _writer, **_kwargs: (
            f"target.attempt_{current_options.attempt_number}.writer_0"
        ),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "read_key_batch",
        lambda _options, _ref, task, _metadata_value, start, stop, **_kwargs: RowBatch(
            ["id"],
            [(task.transfer_slice.index,) for _ordinal in range(start, stop)],
        ),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "insert_target_batch",
        lambda _options, _ref, _stage, batch, _metadata_value, **_kwargs: batch.batch.row_count,
    )
    monkeypatch.setattr(staged_keyed_pipeline, "validate_target_key", validate)
    monkeypatch.setattr(staged_keyed_pipeline, "drop_source_stage", drop)
    monkeypatch.setattr(staged_keyed_pipeline, "_validate_target_stages", lambda *_args: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "_consolidate_created_stages",
        lambda *_args: 0,
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_loaded_stage_row_count",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "finalize_loaded_stage",
        lambda current_options, *_args, **_kwargs: finalized.append(current_options.attempt_number),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "capture_final_target_count", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "cleanup_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_pipeline_start", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_batch_progress", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_key_verification", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_loading_complete", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_transfer_complete", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(transfer_api, "time_print", lambda *_args, **_kwargs: None)

    assert transfer_api.transfer_table("source", "target") == 2

    assert initial_checkpoints == [{}, {}]
    assert len(created_runtimes) == 2
    assert created_runtimes[0] is not created_runtimes[1]
    assert set(created_runtimes[0].verified) == {0}
    assert set(created_runtimes[1].verified) == {0, 1}
    assert len(attempt_states) == 2
    assert attempt_states[0] is not attempt_states[1]
    assert metadata_attempts == [1, 2]
    assert len(set(transfer_ids)) == 1
    assert materialized == {1: [0, 1], 2: [0, 1]}
    assert validated == {1: [0, 1], 2: [0, 1]}
    assert dropped == [(1, 0), (2, 0), (2, 1)]
    assert finalized == [2]


def test_runtime_preserves_first_worker_error_and_unblocks_queue_waiters() -> None:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    runtime = LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=1)
    waiting = threading.Event()

    def wait_for_ready() -> None:
        waiting.set()
        staged_keyed_pipeline._get_with_cancellation(runtime.ready, runtime)

    worker, errors = _thread(wait_for_ready)
    assert waiting.wait(1)
    first = OSError("writer failed")
    runtime.fail(first)
    runtime.fail(ValueError("later reader failure"))
    worker.join(2)

    assert not worker.is_alive()
    assert runtime.first_error is first
    assert runtime.cancellation.is_set()
    assert len(errors) == 1
    with pytest.raises(OSError, match="writer failed"):
        runtime.raise_first_error()


def test_source_cleanup_attempts_every_table_and_preserves_first_error(
    monkeypatch: Any,
) -> None:
    dropped: list[str] = []

    def cleanup(_backend: str, _connection: Any, table: str, **_kwargs: Any) -> None:
        dropped.append(table)
        if table == "first":
            raise OSError("first cleanup")
        if table == "second":
            raise RuntimeError("second cleanup")

    monkeypatch.setattr(staged_keyed_io, "cleanup_stage_table", cleanup)
    with pytest.raises(OSError, match="first cleanup"):
        staged_keyed_io.cleanup_source_stages(
            _options(),
            {"connection": object()},
            ["first", "second", "third"],
        )
    assert dropped == ["first", "second", "third"]
