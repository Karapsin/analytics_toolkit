from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.dml.transfer.flow import (
    staged_attempt,
    staged_keyed_io,
    staged_keyed_logging,
    staged_keyed_pipeline,
    staged_keyed_stream,
)
from analytics_toolkit.sql.dml.transfer.flow.lazy_keyed_runtime import (
    KeyReadComplete,
    LazyKeyedRuntime,
    QueuedKeyBatch,
    ReadyKeyTask,
    VerifiedKey,
    freeze_attempt_metadata,
    make_batch_sizer,
)
from analytics_toolkit.sql.dml.transfer.flow.stage_identity import resolve_internal_columns
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    RowBatch,
    TransferConcurrency,
    TransferConnectionRefs,
    TransferOptions,
    TransferSlice,
    TransferStageState,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


_DEFAULT_PROGRESS_RESULT = object()


def _concurrency(read: int = 1, write: int = 1) -> TransferConcurrency:
    return TransferConcurrency(
        legacy_value=None,
        requested_read=read,
        requested_write=write,
        effective_read=read,
        effective_write=write,
        split_requested=True,
    )


def _options(**overrides: Any) -> TransferOptions:
    values: dict[str, Any] = {
        "from_db_key": "source",
        "from_db_backend": "gp",
        "to_db_key": "target",
        "to_db_backend": "gp",
        "source_sql": "SELECT id FROM source",
        "target_table": "public.target",
        "transfer_id": "a" * 32,
        "canonical_destination_identity": "public.target",
        "destination_hash": "0123456789abcdef",
        "source_transfer_staging_schema": "source_stage",
        "transfer_staging_schema": "target_stage",
        "transfer_slices": [
            TransferSlice(0, (1,), "", "SELECT 1 AS id", "key=1"),
            TransferSlice(1, (2,), "", "SELECT 2 AS id", "key=2"),
        ],
        "transfer_keys": ["key"],
        "batch_size": 2,
        "min_batch_size": 1,
        "max_batch_size": 4,
        "adaptive_batch_size": False,
        "retry_cnt": 2,
        "timeout_increment": 0,
        "transfer_concurrency": _concurrency(),
    }
    values.update(overrides)
    return TransferOptions(**values)


def _metadata() -> Any:
    internal = resolve_internal_columns(["id"], "gp")
    return freeze_attempt_metadata(
        source_columns=["id"],
        source_column_types={"id": "bigint"},
        stage_column_types={
            "id": "BIGINT",
            internal.transfer_id: "TEXT",
            internal.destination_table: "TEXT",
            internal.slice_id: "BIGINT",
            internal.row_ordinal: "BIGINT",
        },
        internal_columns=internal,
    )


def _task(options: TransferOptions, *, expected_rows: int = 1) -> ReadyKeyTask:
    transfer_slice = (options.transfer_slices or [])[0]
    return ReadyKeyTask(
        transfer_slice=transfer_slice,
        source_stage="source.stage_0",
        expected_rows=expected_rows,
        tag="[slice=1/2 key=key:1]",
        materialized_at=0.0,
    )


class _Manager:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.ref: dict[str, Any] = {"connection": object()}
        self.resumed = 0
        self.interrupted = 0
        self.closed = 0

    @contextmanager
    def lease(self, **_kwargs: Any) -> Iterator[dict[str, Any]]:
        yield self.ref

    def run(self, _role: str, operation: Any, **_kwargs: Any) -> Any:
        return operation(self.ref)

    def run_with_connection(
        self,
        role: str,
        open_connection: Any,
        operation: Any,
        **_kwargs: Any,
    ) -> Any:
        del role
        return operation(open_connection())

    def interrupt_active(self) -> None:
        self.interrupted += 1

    def resume_for_cleanup(self) -> None:
        self.resumed += 1

    def close(self) -> None:
        self.closed += 1

    def close_preserving(self, _error: BaseException | None) -> None:
        self.close()


class _ProgressBar:
    def update(self, _rows: int) -> None:
        return

    def close(self) -> None:
        return


class _ConsumeProgress:
    def __init__(
        self,
        *,
        commit_result: Any = _DEFAULT_PROGRESS_RESULT,
        verification: Any = _DEFAULT_PROGRESS_RESULT,
    ) -> None:
        self.commit_result = commit_result
        self.verification = verification

    def snapshot(self) -> Any:
        return SimpleNamespace(committed_rows=0)

    def commit_batch(self, **_kwargs: Any) -> Any:
        return self.commit_result

    def verify_key(self, _key: int) -> Any:
        return self.verification


def _queued(task: ReadyKeyTask, *, row_count: int = 1) -> QueuedKeyBatch:
    return QueuedKeyBatch(
        task=task,
        batch_index=1,
        start_ordinal=1,
        stop_ordinal=1 + row_count,
        batch=RowBatch(["id"], [(index,) for index in range(row_count)]),
        read_started_at=0.0,
        read_completed_at=0.1,
        approximate_memory_bytes=16,
    )


@dataclass(frozen=True)
class _ConsumeCase:
    stage_table: str | None
    progress: Any
    inserted_rows: int = 1


def _consume(
    monkeypatch: pytest.MonkeyPatch,
    options: TransferOptions,
    runtime: LazyKeyedRuntime,
    task: ReadyKeyTask,
    case: _ConsumeCase,
) -> None:
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "insert_target_batch",
        lambda *_args, **_kwargs: case.inserted_rows,
    )
    monkeypatch.setattr(staged_keyed_pipeline, "validate_target_key", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_batch_progress", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_key_verification", lambda *_args: None)
    staged_keyed_pipeline._consume_key(
        options,
        _metadata(),
        TransferStageState(target_exists=True),
        runtime,
        _Manager(),
        case.progress,
        threading.Lock(),
        0,
        case.stage_table,
        task,
        make_batch_sizer(options),
        1,
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


def test_keyed_io_drop_retry_replaces_failed_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(retry_cnt=2)
    task = _task(options)
    attempts: list[Any] = []
    rollbacks: list[Any] = []
    replacements: list[tuple[str, Any]] = []
    first_connection = object()
    second_connection = object()
    source_ref: dict[str, Any] = {"connection": first_connection}

    def cleanup(_backend: str, connection: Any, _stage: str, **_kwargs: Any) -> None:
        attempts.append(connection)
        if connection is first_connection:
            message = "connection lost during drop"
            raise OSError(message)

    def replace(key: str, ref: dict[str, Any]) -> None:
        replacements.append((key, ref["connection"]))
        ref["connection"] = second_connection

    source_ref["bounded_replace_connection"] = replace
    monkeypatch.setattr(staged_keyed_io, "cleanup_stage_table", cleanup)
    monkeypatch.setattr(staged_keyed_io, "rollback_quietly", rollbacks.append)

    staged_keyed_io.drop_source_stage(options, source_ref, task)

    assert attempts == [first_connection, second_connection]
    assert rollbacks == [first_connection]
    assert replacements == [("source", first_connection)]


def test_keyed_io_cleanup_attempts_every_stage_and_preserves_first_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options()
    calls: list[str] = []
    first = OSError("first cleanup failed")

    def cleanup(_backend: str, _connection: Any, stage: str, **_kwargs: Any) -> None:
        calls.append(stage)
        if stage == "stage.a":
            raise first

    monkeypatch.setattr(staged_keyed_io, "cleanup_stage_table", cleanup)

    with pytest.raises(OSError, match="first cleanup failed") as exc_info:
        staged_keyed_io.cleanup_source_stages(
            options,
            {"connection": object()},
            ["stage.a", "stage.b"],
        )

    assert exc_info.value is first
    assert calls == ["stage.a", "stage.b"]

    calls.clear()
    monkeypatch.setattr(
        staged_keyed_io,
        "cleanup_stage_table",
        lambda _backend, _connection, stage, **_kwargs: calls.append(stage),
    )
    staged_keyed_io.cleanup_source_stages(
        options,
        {"connection": object()},
        ["stage.a", "stage.b"],
    )
    assert calls == ["stage.a", "stage.b"]


def test_failed_empty_source_cleanup_includes_zero_keys_and_preserves_first_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(transfer_concurrency=_concurrency(1, 2))
    runtime = LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=2)
    stages = ["source.unpublished", "source.zero", "source.nonempty"]
    for stage in stages:
        assert runtime.live_stage_credits.acquire(blocking=False)
        runtime.reserve_source_stage(stage)
    zero = _task(options, expected_rows=0)
    zero.source_stage = stages[1]
    nonempty = _task(options, expected_rows=1)
    nonempty.source_stage = stages[2]
    runtime.publish_source_stage(zero)
    runtime.publish_source_stage(nonempty)
    manager = _Manager()
    calls: list[str] = []
    first = OSError("first empty cleanup failed")

    def cleanup(
        _options: TransferOptions,
        _source_ref: dict[str, Any],
        stage_tables: list[str],
    ) -> None:
        calls.extend(stage_tables)
        if stage_tables[0] == stages[0]:
            raise first

    monkeypatch.setattr(staged_keyed_io, "cleanup_source_stages", cleanup)

    with pytest.raises(OSError, match="first empty cleanup failed") as exc_info:
        staged_keyed_io.cleanup_failed_empty_source_stages(options, runtime, manager)

    assert exc_info.value is first
    assert manager.resumed == 1
    assert calls == stages[:2]
    assert stages[0] in runtime.source_stage_tables
    assert stages[1] not in runtime.source_stage_tables
    assert stages[2] in runtime.source_stage_tables


def test_keyed_io_consolidation_count_final_count_host_runner_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(collect_final_target_count=True)
    runtime = LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=2)
    runtime.mark_verified(VerifiedKey(0, 3, 3, "stage.primary"))
    runtime.mark_verified(VerifiedKey(1, 7, 7, "stage.secondary"))
    manager = _Manager()
    consolidated: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        staged_attempt,
        "_consolidate_worker_stages",
        lambda _options, _ref, _state, stages: consolidated.append(tuple(stages)),
    )

    copied = staged_keyed_io.consolidate_created_stages(
        options,
        manager,
        TransferStageState(target_exists=True),
        ["stage.primary", "stage.secondary"],
        runtime,
    )
    assert copied == 7
    assert consolidated == [("stage.primary", "stage.secondary")]

    monkeypatch.setattr(
        staged_keyed_io,
        "best_effort_transfer_target_count",
        lambda _options, target_connection_runner: target_connection_runner(
            "final count",
            lambda _ref: 11,
        ),
    )
    staged_keyed_io.capture_final_target_count(options, manager)
    assert options.final_target_rows == 11

    monkeypatch.setattr(
        staged_keyed_io,
        "get_ch_connection_for_host",
        lambda key, host: f"{key}:{host}",
    )
    host_runner = staged_keyed_io.make_target_host_connection_runner(options, manager)
    assert host_runner("host-a", lambda connection: f"used {connection}") == ("used target:host-a")

    replacements: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        staged_keyed_io,
        "replace_connection",
        lambda key, ref: replacements.append((key, ref)),
    )
    connection_ref: dict[str, Any] = {"connection": object()}
    staged_keyed_io._replace_managed_connection("source", connection_ref)
    assert replacements == [("source", connection_ref)]


def test_prepare_attempt_reads_existing_target_insert_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(replace_target_table=False, write_mode="append")
    state = TransferStageState(target_exists=True)
    refs = TransferConnectionRefs(
        source={"connection": object()},
        target={"connection": object()},
    )
    insert_contracts: list[dict[str, str]] = []
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "inspect_source_query_schema",
        lambda *_args: [SourceColumn("id", "bigint")],
    )
    monkeypatch.setattr(
        staged_keyed_pipeline, "cleanup_superseded_transfer_stages", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "map_source_schema_to_target",
        lambda *_args, **_kwargs: {"id": "BIGINT"},
    )
    monkeypatch.setattr(staged_keyed_pipeline, "ensure_transfer_target_table", lambda *_args: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "get_existing_target_insert_types",
        lambda _backend, _connection, _table, source_types, **_kwargs: (
            insert_contracts.append(source_types) or {"id": "INTEGER"}
        ),
    )

    staged_keyed_pipeline._prepare_attempt(options, refs, state)

    assert insert_contracts == [{"id": "BIGINT"}]
    assert state.insert_column_types == {"id": "INTEGER"}


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_reader_materialization_failure_cleans_exact_reserved_stage_when_possible(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_fails: bool,
) -> None:
    options = _options(transfer_slices=[_options().transfer_slices[0]])
    runtime = LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=1)
    logs: list[str] = []
    cleaned: list[str] = []

    def cleanup(
        _options: TransferOptions,
        _source_ref: dict[str, Any],
        stages: list[str],
    ) -> None:
        cleaned.extend(stages)
        if cleanup_fails:
            message = "cleanup also failed"
            raise OSError(message)

    monkeypatch.setattr(staged_keyed_pipeline, "_drain_drop_ready", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "allocate_source_stage_name",
        lambda *_args: "source.reserved",
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "materialize_source_key",
        lambda *_args: (_ for _ in ()).throw(OSError("CTAS failed")),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "cleanup_source_stages", cleanup)
    monkeypatch.setattr(staged_keyed_pipeline, "time_print", logs.append)

    with pytest.raises(OSError, match="CTAS failed"):
        staged_keyed_pipeline._reader_worker(
            options,
            _metadata(),
            runtime,
            _Manager(),
            SimpleNamespace(start_key=lambda _key: None),
            threading.Lock(),
            0,
        )

    assert cleaned == ["source.reserved"]
    assert runtime.cancellation.is_set()
    assert ("source.reserved" in runtime.source_stage_tables) is cleanup_fails
    assert any("could not be removed" in message for message in logs) is cleanup_fails


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


def test_collect_worker_and_live_stage_credit_propagate_cancellation() -> None:
    runtime = LazyKeyedRuntime([], read_workers=1, write_workers=1)
    failure = RuntimeError("worker failed")
    future = SimpleNamespace(result=lambda: (_ for _ in ()).throw(failure))

    staged_keyed_pipeline._collect_workers([SimpleNamespace(result=lambda: None), future], runtime)

    assert runtime.first_error is failure
    runtime.cancellation.set()
    with pytest.raises(RuntimeError, match="scheduling was cancelled"):
        staged_keyed_pipeline._acquire_live_stage_credit(_options(), runtime, _Manager())


def test_live_stage_credit_drains_acknowledgements_before_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = LazyKeyedRuntime([], read_workers=1, write_workers=1)
    assert runtime.live_stage_credits.acquire(blocking=False)
    assert runtime.live_stage_credits.acquire(blocking=False)
    drains: list[int | None] = []

    def drain(*_args: Any, limit: int | None, **_kwargs: Any) -> int:
        drains.append(limit)
        runtime.live_stage_credits.release()
        return 1

    monkeypatch.setattr(staged_keyed_pipeline, "_drain_drop_ready", drain)

    staged_keyed_pipeline._acquire_live_stage_credit(_options(), runtime, _Manager())

    assert drains == [None]


def test_cancelled_writer_exits_without_claiming_a_ready_key() -> None:
    options = _options()
    runtime = LazyKeyedRuntime(options.transfer_slices or [], read_workers=1, write_workers=1)
    runtime.cancellation.set()

    staged_keyed_pipeline._writer_worker(
        options,
        _metadata(),
        TransferStageState(target_exists=True),
        runtime,
        _Manager(),
        SimpleNamespace(),
        threading.Lock(),
        0,
        1,
    )

    assert runtime.ready.empty()


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


def _patch_attempt_shell(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_workers: Any,
    cleanup_stage: Any,
    cleanup_source: Any = lambda *_args: None,
    messages: list[str] | None = None,
) -> tuple[TransferOptions, TransferStageState]:
    options = _options(transfer_concurrency=_concurrency(1, 1))
    state = TransferStageState(target_exists=True, stage_table_created=True)
    monkeypatch.setattr(staged_keyed_pipeline, "BoundedConnectionManager", _Manager)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "make_transfer_progress_bar",
        lambda *_args, **_kwargs: _ProgressBar(),
    )
    monkeypatch.setattr(staged_keyed_pipeline, "create_stage_state", lambda *_args: state)
    monkeypatch.setattr(staged_keyed_pipeline, "_prepare_attempt", lambda *_args: _metadata())
    monkeypatch.setattr(staged_keyed_pipeline, "_run_lazy_workers", run_workers)
    monkeypatch.setattr(staged_keyed_pipeline, "_sync_stage_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_keyed_pipeline, "_validate_target_stages", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "_consolidate_created_stages", lambda *_args: 0)
    monkeypatch.setattr(
        staged_keyed_pipeline, "validate_loaded_stage_row_count", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        staged_keyed_pipeline, "finalize_loaded_stage", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(staged_keyed_pipeline, "capture_final_target_count", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "cleanup_stage", cleanup_stage)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "cleanup_failed_empty_source_stages",
        cleanup_source,
    )
    monkeypatch.setattr(staged_keyed_pipeline, "log_pipeline_start", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_loading_complete", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_transfer_complete", lambda *_args: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "time_print",
        (lambda message, **_kwargs: messages.append(message))
        if messages is not None
        else (lambda *_args, **_kwargs: None),
    )
    return options, state


def test_attempt_cleanup_failure_after_success_is_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def complete_loading(
        current_options: TransferOptions,
        _metadata_value: Any,
        _stage_state: TransferStageState,
        _runtime: LazyKeyedRuntime,
        _source_connections: Any,
        _target_connections: Any,
        progress: Any,
        **_kwargs: Any,
    ) -> None:
        for transfer_slice in current_options.transfer_slices or []:
            progress.start_key(transfer_slice.index)
            progress.materialize_key(transfer_slice.index, 0)
            progress.assign_key(transfer_slice.index, 0)
            progress.verify_key(transfer_slice.index)

    options, _state = _patch_attempt_shell(
        monkeypatch,
        run_workers=complete_loading,
        cleanup_stage=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("target cleanup failed")
        ),
    )

    with pytest.raises(staged_keyed_pipeline.FinalizedTargetCleanupError) as exc_info:
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=1,
        )

    assert exc_info.value.analytics_toolkit_sql_retry_safe is False
    assert "after destination finalization" in str(exc_info.value)


def test_attempt_preserves_original_error_and_marks_failed_cleanup_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = RuntimeError("worker failed")
    messages: list[str] = []
    source_cleanup_calls = 0

    def fail_workers(*_args: Any, **_kwargs: Any) -> None:
        raise original

    def fail_source_cleanup(*_args: Any) -> None:
        nonlocal source_cleanup_calls
        source_cleanup_calls += 1
        message = "source cleanup failed"
        raise OSError(message)

    options, _state = _patch_attempt_shell(
        monkeypatch,
        run_workers=fail_workers,
        cleanup_stage=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("target cleanup failed")
        ),
        cleanup_source=fail_source_cleanup,
        messages=messages,
    )

    with pytest.raises(RuntimeError) as exc_info:
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=1,
        )

    assert exc_info.value is original
    assert original.analytics_toolkit_sql_retry_safe is False
    assert source_cleanup_calls == 1
    assert any("empty attempt-owned source stage" in message for message in messages)
    assert any("Cleanup failed while handling" in message for message in messages)


@pytest.mark.parametrize("error_kind", ["no_dict", "no_note"])
def test_attempt_cleanup_metadata_failure_uses_cleanup_error_precedence(
    monkeypatch: pytest.MonkeyPatch,
    error_kind: str,
) -> None:
    class NoDictError(RuntimeError):
        @property
        def __dict__(self) -> dict[str, Any]:
            message = "error metadata is immutable"
            raise AttributeError(message)

    class NoNoteError(RuntimeError):
        add_note = None

    original = (
        NoDictError("worker failed") if error_kind == "no_dict" else NoNoteError("worker failed")
    )
    options, _state = _patch_attempt_shell(
        monkeypatch,
        run_workers=lambda *_args, **_kwargs: (_ for _ in ()).throw(original),
        cleanup_stage=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("target cleanup failed")
        ),
    )

    expected = (
        staged_keyed_pipeline.FinalizedTargetCleanupError
        if error_kind == "no_dict"
        else NoNoteError
    )
    with pytest.raises(expected):
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=1,
        )

    if error_kind == "no_note":
        assert original.analytics_toolkit_sql_retry_safe is False


def test_attempt_error_before_stage_state_skips_target_stage_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options()
    original = RuntimeError("stage state creation failed")
    cleanup_calls: list[str] = []
    monkeypatch.setattr(staged_keyed_pipeline, "BoundedConnectionManager", _Manager)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "make_transfer_progress_bar",
        lambda *_args, **_kwargs: _ProgressBar(),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "create_stage_state",
        lambda *_args: (_ for _ in ()).throw(original),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "cleanup_failed_empty_source_stages",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "cleanup_stage",
        lambda *_args, **_kwargs: cleanup_calls.append("target stage"),
    )

    with pytest.raises(RuntimeError) as exc_info:
        staged_keyed_pipeline.run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=1,
        )

    assert exc_info.value is original
    assert cleanup_calls == []


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
