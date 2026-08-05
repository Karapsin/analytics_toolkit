from __future__ import annotations

import queue
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.dml.transfer.runtime.models import AdaptiveBatchSizer
from analytics_toolkit.sql.execution.cancellation import raise_if_cancelled

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from analytics_toolkit.sql.dml.transfer.flow.stage_identity import (
        TransferInternalColumns,
    )
    from analytics_toolkit.sql.dml.transfer.runtime.models import (
        RowBatch,
        TransferOptions,
        TransferSlice,
    )


class AcknowledgedSourceStageDropError(RuntimeError):
    analytics_toolkit_sql_retry_safe = False


class FinalizedTargetCleanupError(AcknowledgedSourceStageDropError): ...


@dataclass(frozen=True)
class AttemptMetadata:
    source_columns: tuple[str, ...]
    source_column_types: Mapping[str, str | None]
    stage_column_types: Mapping[str, str] | None
    internal_columns: TransferInternalColumns


@dataclass
class ReadyKeyTask:
    transfer_slice: TransferSlice
    source_stage: str
    expected_rows: int
    tag: str
    materialized_at: float
    assignment: threading.Event = field(default_factory=threading.Event)
    writer_index: int | None = None
    batch_queue: queue.Queue[QueuedKeyBatch | KeyReadComplete] | None = None
    batch_slot: threading.BoundedSemaphore | None = None
    batch_size: int | None = None


@dataclass
class QueuedKeyBatch:
    task: ReadyKeyTask
    batch_index: int
    start_ordinal: int
    stop_ordinal: int
    batch: RowBatch
    read_started_at: float
    read_completed_at: float
    approximate_memory_bytes: int
    queued_at: float | None = None
    prefetch_slot: threading.BoundedSemaphore | None = None

    @property
    def logical_id(self) -> tuple[int, int, int, int]:
        return (
            self.task.transfer_slice.index,
            self.batch_index,
            self.start_ordinal,
            self.stop_ordinal,
        )


@dataclass(frozen=True)
class KeyReadComplete:
    task: ReadyKeyTask
    streamed_rows: int
    batch_count: int


@dataclass(frozen=True)
class DropReady:
    task: ReadyKeyTask
    target_stage: str | None


@dataclass(frozen=True)
class VerifiedKey:
    slice_index: int
    expected_rows: int
    streamed_rows: int
    target_stage: str | None


class LazyKeyedRuntime:
    def __init__(
        self,
        transfer_slices: list[TransferSlice],
        *,
        read_workers: int,
        write_workers: int,
    ) -> None:
        self.pending = deque(transfer_slices)
        self.ready: queue.Queue[ReadyKeyTask | None] = queue.Queue(
            maxsize=max(1, write_workers),
        )
        self.writer_queues: list[queue.Queue[QueuedKeyBatch | KeyReadComplete]] = [
            queue.Queue(maxsize=1) for _ in range(write_workers)
        ]
        self.writer_batch_slots = [threading.BoundedSemaphore(1) for _ in range(write_workers)]
        self.drop_ready: queue.Queue[DropReady] = queue.Queue()
        self.live_stage_limit = read_workers + write_workers
        self.live_stage_credits = threading.BoundedSemaphore(self.live_stage_limit)
        self.cancellation = threading.Event()
        self._lock = threading.Lock()
        self._first_error: BaseException | None = None
        self._source_stages: dict[str, ReadyKeyTask | None] = {}
        self._target_stages: dict[int, str] = {}
        self._target_stage_candidates: set[str] = set()
        self._verified: dict[int, VerifiedKey] = {}
        self._successful_batches: set[tuple[int, int, int, int]] = set()
        self._failure_callbacks: list[Callable[[], None]] = []
        self.source_stages_dropped = 0

    @property
    def first_error(self) -> BaseException | None:
        with self._lock:
            return self._first_error

    @property
    def live_source_stage_count(self) -> int:
        with self._lock:
            return len(self._source_stages)

    @property
    def verified(self) -> dict[int, VerifiedKey]:
        with self._lock:
            return dict(self._verified)

    @property
    def target_stages(self) -> dict[int, str]:
        with self._lock:
            return dict(self._target_stages)

    @property
    def source_stage_tables(self) -> list[str]:
        with self._lock:
            return list(self._source_stages)

    @property
    def source_stage_entries(self) -> dict[str, ReadyKeyTask | None]:
        with self._lock:
            return dict(self._source_stages)

    @property
    def target_stage_candidates(self) -> set[str]:
        with self._lock:
            return set(self._target_stage_candidates)

    def fail(self, exc: BaseException) -> None:
        callbacks: list[Callable[[], None]] = []
        with self._lock:
            if self._first_error is None:
                self._first_error = exc
                callbacks = list(self._failure_callbacks)
        self.cancellation.set()
        for callback in callbacks:
            with suppress(Exception):
                callback()

    def add_failure_callback(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._failure_callbacks.append(callback)

    def raise_first_error(self) -> None:
        error = self.first_error
        if error is not None:
            raise error.with_traceback(error.__traceback__)

    def claim_pending(self) -> TransferSlice | None:
        with self._lock:
            return self.pending.popleft() if self.pending else None

    def reserve_source_stage(self, table: str) -> None:
        with self._lock:
            if table in self._source_stages:
                message = f"Source stage was reserved twice: {table}"
                raise RuntimeError(message)
            self._source_stages[table] = None

    def publish_source_stage(self, task: ReadyKeyTask) -> None:
        with self._lock:
            if task.source_stage not in self._source_stages:
                message = "Source stage was not reserved before publication."
                raise RuntimeError(message)
            self._source_stages[task.source_stage] = task

    def mark_source_stage_dropped(self, table: str) -> None:
        with self._lock:
            if table not in self._source_stages:
                return
            del self._source_stages[table]
            self.source_stages_dropped += 1
        self.live_stage_credits.release()

    def register_target_stage(self, writer_index: int, table: str) -> None:
        with self._lock:
            existing = self._target_stages.get(writer_index)
            if existing is not None and existing != table:
                message = f"Target writer {writer_index} created two stages."
                raise RuntimeError(message)
            self._target_stages[writer_index] = table

    def register_target_stage_candidate(self, table: str) -> None:
        with self._lock:
            self._target_stage_candidates.add(table)

    def mark_batch_success(self, logical_id: tuple[int, int, int, int]) -> None:
        with self._lock:
            if logical_id in self._successful_batches:
                message = f"Logical transfer batch was committed twice: {logical_id}"
                raise RuntimeError(message)
            self._successful_batches.add(logical_id)

    def mark_verified(self, checkpoint: VerifiedKey) -> None:
        with self._lock:
            if checkpoint.slice_index in self._verified:
                message = f"Transfer key {checkpoint.slice_index} was verified twice."
                raise RuntimeError(message)
            self._verified[checkpoint.slice_index] = checkpoint


def freeze_attempt_metadata(
    *,
    source_columns: list[str],
    source_column_types: dict[str, str | None],
    stage_column_types: dict[str, str] | None,
    internal_columns: TransferInternalColumns,
) -> AttemptMetadata:
    return AttemptMetadata(
        source_columns=tuple(source_columns),
        source_column_types=MappingProxyType(dict(source_column_types)),
        stage_column_types=(
            MappingProxyType(dict(stage_column_types)) if stage_column_types is not None else None
        ),
        internal_columns=internal_columns,
    )


def make_batch_sizer(options: TransferOptions) -> AdaptiveBatchSizer:
    resident_batch_slots = max(1, options.transfer_concurrency.effective_write * 2)
    return AdaptiveBatchSizer(
        enabled=options.adaptive_batch_size,
        current_size=options.batch_size,
        min_size=options.min_batch_size,
        max_size=options.max_batch_size,
        target_seconds=options.target_batch_seconds,
        min_target_seconds=options.min_batch_seconds,
        max_target_seconds=options.max_batch_seconds,
        optimize_by_rows_per_second=options.target_rows_per_second,
        target_rows_per_second_window=options.target_rows_per_second_window,
        target_rows_per_second_deadband=options.target_rows_per_second_deadband,
        adaptive_batch_size_step=options.adaptive_batch_size_step,
        target_memory_bytes=_per_resident_batch_memory(
            options.target_batch_memory_bytes,
            resident_batch_slots,
        ),
        min_target_memory_bytes=_per_resident_batch_memory(
            options.min_batch_memory_bytes,
            resident_batch_slots,
        ),
        max_target_memory_bytes=_per_resident_batch_memory(
            options.max_batch_memory_bytes,
            resident_batch_slots,
        ),
    )


def _per_resident_batch_memory(value: int | None, slots: int) -> int | None:
    return None if value is None else max(1, value // slots)


def wait_for_assignment(task: ReadyKeyTask, runtime: LazyKeyedRuntime) -> None:
    while not task.assignment.wait(timeout=0.1):
        _raise_if_stopped(runtime, "Target writer assignment was cancelled.")


def acquire_batch_slot_with_cancellation(
    slot: threading.BoundedSemaphore,
    runtime: LazyKeyedRuntime,
) -> None:
    """Reserve one writer prefetch slot before allocating a source batch."""
    while True:
        _raise_if_stopped(runtime, "Batch prefetch wait was cancelled.")
        if slot.acquire(timeout=0.1):
            return


def release_queued_batch_slot(batch: QueuedKeyBatch) -> None:
    """Transfer a dequeued batch's prefetch credit back to its reader."""
    slot = batch.prefetch_slot
    if slot is None:
        return
    batch.prefetch_slot = None
    slot.release()


def put_batch_with_cancellation(
    destination: queue.Queue[Any],
    item: QueuedKeyBatch,
    runtime: LazyKeyedRuntime,
) -> None:
    while True:
        _raise_if_stopped(runtime, "Batch handoff was cancelled.")
        item.queued_at = time.monotonic()
        try:
            destination.put(item, timeout=0.1)
        except queue.Full:
            continue
        else:
            return


def put_with_cancellation(
    destination: queue.Queue[Any],
    item: Any,
    runtime: LazyKeyedRuntime,
) -> None:
    while True:
        _raise_if_stopped(runtime, "Pipeline queue handoff was cancelled.")
        try:
            destination.put(item, timeout=0.1)
        except queue.Full:
            continue
        else:
            return


def get_with_cancellation(
    source: queue.Queue[Any],
    runtime: LazyKeyedRuntime,
) -> Any:
    while True:
        _raise_if_stopped(runtime, "Pipeline queue wait was cancelled.")
        try:
            return source.get(timeout=0.1)
        except queue.Empty:
            continue


def _raise_if_stopped(runtime: LazyKeyedRuntime, message: str) -> None:
    if runtime.cancellation.is_set():
        raise RuntimeError(message)
    raise_if_cancelled()
