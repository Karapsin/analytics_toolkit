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


__all__ = [
    "TYPE_CHECKING",
    "_DEFAULT_PROGRESS_RESULT",
    "Any",
    "KeyReadComplete",
    "LazyKeyedRuntime",
    "QueuedKeyBatch",
    "ReadyKeyTask",
    "RowBatch",
    "SimpleNamespace",
    "SourceColumn",
    "TransferConcurrency",
    "TransferConnectionRefs",
    "TransferOptions",
    "TransferSlice",
    "TransferStageState",
    "VerifiedKey",
    "_ConsumeCase",
    "_ConsumeProgress",
    "_Manager",
    "_ProgressBar",
    "_concurrency",
    "_consume",
    "_metadata",
    "_options",
    "_patch_attempt_shell",
    "_queued",
    "_task",
    "contextmanager",
    "dataclass",
    "freeze_attempt_metadata",
    "make_batch_sizer",
    "pytest",
    "resolve_internal_columns",
    "staged_attempt",
    "staged_keyed_io",
    "staged_keyed_logging",
    "staged_keyed_pipeline",
    "staged_keyed_stream",
    "threading",
]
