from __future__ import annotations

import math
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.dml.transfer.flow.transfer_progress import (
    BatchProgress,
    BatchTiming,
    TransferProgressSnapshot,
    TransferProgressTracker,
    format_duration,
    format_eta,
    format_memory_rate,
    format_rows_rate,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable

    from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch, TransferOptions

_UNKEYED_KEY_ID = 0
_UNKEYED_TAG = "[slice=1/1]"
_MINIMUM_ETA_BATCHES = 2
_MINIMUM_MULTIWORKER_COUNT = 2


class UnkeyedStagedProgress:
    """Attempt-scoped progress for one exact, unkeyed source snapshot."""

    def __init__(  # noqa: PLR0913
        self,
        options: TransferOptions,
        *,
        total_rows: int,
        worker_count: int,
        attempt_started_at: float,
        progress_bar: Any,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(total_rows, bool) or not isinstance(total_rows, int) or total_rows < 0:
            msg = "total_rows must be a built-in non-negative integer."
            raise ValueError(msg)
        if isinstance(worker_count, bool) or not isinstance(worker_count, int) or worker_count < 1:
            msg = "worker_count must be a built-in positive integer."
            raise ValueError(msg)
        if not math.isfinite(attempt_started_at):
            msg = "attempt_started_at must be finite."
            raise ValueError(msg)
        self._options = options
        self._total_rows = total_rows
        self._worker_count = worker_count
        self._clock = clock
        self._progress_bar = progress_bar
        self._lock = threading.Lock()
        self._next_batch_index = 1
        self._writer_stage_rows: dict[int, int] = {}
        self._finalization_started_at: float | None = None
        self._tracker = TransferProgressTracker(
            total_key_count=1,
            active_writers=worker_count,
            consolidation_enabled=options.write_mode != "upsert",
            allow_split_key_writers=True,
            attempt_number=options.attempt_number,
            clock=clock,
            progress_bar=progress_bar,
        )
        self._tracker.reset(
            attempt_number=options.attempt_number,
            started_at=attempt_started_at,
        )
        self._tracker.start_key(_UNKEYED_KEY_ID, started_at=attempt_started_at)
        self._tracker.materialize_key(
            _UNKEYED_KEY_ID,
            total_rows,
            started_at=attempt_started_at,
        )
        time_print(
            f"{_UNKEYED_TAG} Materialized {total_rows:,} source rows; exact load total established"
        )

    def now(self) -> float:
        return float(self._clock())

    @property
    def committed_rows(self) -> int:
        return self._tracker.snapshot().committed_rows

    @property
    def log_prefix(self) -> str:
        return f"{_UNKEYED_TAG} "

    @property
    def expected_consolidation_rows(self) -> int:
        if self._options.write_mode == "upsert" or self._worker_count < _MINIMUM_MULTIWORKER_COUNT:
            return 0
        with self._lock:
            return sum(
                rows for writer_id, rows in self._writer_stage_rows.items() if writer_id != 0
            )

    def commit_batch(  # noqa: PLR0913
        self,
        *,
        logical_batch_id: Hashable,
        worker_id: int,
        batch: RowBatch,
        read_started_at: float,
        read_completed_at: float,
        insert_completed_at: float,
    ) -> BatchProgress:
        timing = BatchTiming(
            read_started_at=read_started_at,
            read_completed_at=read_completed_at,
            queued_at=read_completed_at,
            insert_completed_at=insert_completed_at,
            approximate_memory_bytes=batch.approx_memory_bytes(),
        )
        with self._lock:
            batch_index = self._next_batch_index
            self._next_batch_index += 1
            progress = self._tracker.commit_batch(
                logical_batch_id=logical_batch_id,
                key_id=_UNKEYED_KEY_ID,
                batch_index=batch_index,
                rows=batch.row_count,
                timing=timing,
                writer_id=worker_id,
            )
            if progress is None:
                msg = "Unkeyed staged logical batch was committed twice."
                raise RuntimeError(msg)
            self._writer_stage_rows[worker_id] = (
                self._writer_stage_rows.get(worker_id, 0) + batch.row_count
            )
            self._log_batch(progress)
        return progress

    def target_insert_retry_status(
        self,
        logical_batch_id: tuple[int, int, int],
        attempt: int,
        total_attempts: int,
    ) -> str:
        slice_id, start_ordinal, stop_ordinal = logical_batch_id
        return (
            f"Retrying target-stage range {slice_id}:{start_ordinal}-{stop_ordinal} insert: "
            f"attempt {attempt}/{total_attempts}; committed total remains "
            f"{self.committed_rows:,} rows; ETA unchanged"
        )

    def attach_attempt_summary(self, error: BaseException, phase: str) -> None:
        snapshot = self._tracker.snapshot()
        _attach_attempt_summary(
            error,
            phase=phase,
            committed_rows=snapshot.committed_rows,
            elapsed_seconds=snapshot.attempt_elapsed_seconds,
        )

    def mark_loading_complete(self) -> TransferProgressSnapshot:
        verification = self._tracker.verify_key(_UNKEYED_KEY_ID)
        if verification is None:
            msg = "Unkeyed staged source was verified twice."
            raise RuntimeError(msg)
        snapshot = self._tracker.mark_loading_complete()
        snapshot = self._with_actual_consolidation(
            snapshot,
            self.expected_consolidation_rows,
        )
        time_print(
            f"Completed source-stage loading: {snapshot.committed_rows:,} rows in "
            f"{format_duration(snapshot.attempt_elapsed_seconds)}; average rate "
            f"{format_rows_rate(snapshot.average_rows_per_second)}; average approximate "
            "RAM rate "
            f"{format_memory_rate(snapshot.average_memory_bytes_per_second)}; remaining "
            "total transfer ETA "
            f"{format_eta(snapshot.total_transfer_eta_seconds, approximate=True)}"
        )
        return snapshot

    def mark_consolidation_complete(
        self,
        *,
        stage_count: int,
        copied_rows: int,
        elapsed_seconds: float,
    ) -> TransferProgressSnapshot:
        self._tracker.record_consolidated_rows(
            logical_operation_id="unkeyed-writer-stage-consolidation",
            rows=copied_rows,
        )
        snapshot = self._tracker.mark_consolidation_complete()
        time_print(
            "Completed target-stage consolidation: "
            f"{stage_count} writer stages, {copied_rows:,} copied rows in "
            f"{format_duration(elapsed_seconds)}; remaining total transfer ETA "
            f"{format_eta(snapshot.total_transfer_eta_seconds, approximate=True)}"
        )
        return snapshot

    def mark_finalization_started(self) -> TransferProgressSnapshot:
        snapshot = self._tracker.mark_finalization_started()
        self._finalization_started_at = self.now()
        time_print(
            f"Starting destination finalization: mode {self._options.write_mode}; "
            f"{self._total_rows:,} rows; total transfer ETA "
            f"{format_eta(snapshot.total_transfer_eta_seconds, approximate=True)}"
        )
        return snapshot

    def mark_finalization_complete(self) -> TransferProgressSnapshot:
        snapshot = self._tracker.mark_finalization_complete()
        started_at = self._finalization_started_at
        if started_at is None:
            msg = "Unkeyed staged finalization did not record its start time."
            raise RuntimeError(msg)
        time_print(
            f"Completed destination finalization: mode {self._options.write_mode}; "
            f"{self._total_rows:,} rows in {format_duration(self.now() - started_at)}"
        )
        return snapshot

    def log_transfer_complete(
        self,
        *,
        source_stages_dropped: int,
        target_stages_cleaned: int,
    ) -> None:
        snapshot = self._tracker.snapshot()
        if snapshot.committed_rows == 0:
            time_print(
                f"Completed transfer: 0 rows from {self._options.from_db_key} to "
                f"{self._options.to_db_key} in "
                f"{format_duration(snapshot.attempt_elapsed_seconds)}; no batch throughput; "
                "load ETA 0 seconds; total transfer ETA 0 seconds"
            )
            return
        time_print(
            f"Completed transfer: {snapshot.committed_rows:,} rows from "
            f"{self._options.from_db_key} to {self._options.to_db_key} in "
            f"{format_duration(snapshot.attempt_elapsed_seconds)}; average staged rate "
            f"{format_rows_rate(snapshot.average_rows_per_second)}; average approximate RAM "
            f"rate {format_memory_rate(snapshot.average_memory_bytes_per_second)}; source "
            f"stages dropped {source_stages_dropped}/1; target stages cleaned "
            f"{target_stages_cleaned}/{target_stages_cleaned}"
        )

    def close(self) -> None:
        self._progress_bar.close()

    def snapshot(self) -> TransferProgressSnapshot:
        return self._tracker.snapshot()

    def _log_batch(self, progress: BatchProgress) -> None:
        snapshot = progress.snapshot
        load_eta = (
            snapshot.load_eta_seconds
            if snapshot.successful_batch_count >= _MINIMUM_ETA_BATCHES
            else None
        )
        total_eta = (
            snapshot.total_transfer_eta_seconds
            if snapshot.successful_batch_count >= _MINIMUM_ETA_BATCHES
            else None
        )
        time_print(
            f"{_UNKEYED_TAG} Staged batch {progress.batch_index}: "
            f"{progress.batch_rows:,} rows; total {snapshot.committed_rows:,}/"
            f"{self._total_rows:,}; batch time {format_duration(progress.timing.batch_seconds)}; "
            f"total time {format_duration(snapshot.attempt_elapsed_seconds)}; batch rate "
            f"{format_rows_rate(progress.batch_rows_per_second)}; rolling rate "
            f"{format_rows_rate(snapshot.rolling_rows_per_second)}; approximate RAM rate "
            f"{format_memory_rate(progress.batch_memory_bytes_per_second)}; rolling "
            "approximate RAM rate "
            f"{format_memory_rate(snapshot.rolling_memory_bytes_per_second)}; load ETA "
            f"{format_eta(load_eta, approximate=False)}; total transfer ETA "
            f"{format_eta(total_eta, approximate=True)}"
        )

    @staticmethod
    def _with_actual_consolidation(
        snapshot: TransferProgressSnapshot,
        consolidation_rows: int,
    ) -> TransferProgressSnapshot:
        finalization_rows = snapshot.remaining_finalization_rows
        total_remaining = (
            None if finalization_rows is None else consolidation_rows + finalization_rows
        )
        total_eta = _eta_seconds(total_remaining, snapshot.eta_rows_per_second)
        return replace(
            snapshot,
            remaining_consolidation_rows=consolidation_rows,
            total_transfer_eta_seconds=total_eta,
        )


def _eta_seconds(rows: int | None, rate: float | None) -> float | None:
    if rows is None:
        return None
    if rows == 0:
        return 0.0
    if rate is None or rate <= 0:
        return None
    return rows / rate


def _attach_attempt_summary(
    error: BaseException,
    *,
    phase: str,
    committed_rows: int,
    elapsed_seconds: float,
) -> None:
    try:
        error.__dict__["analytics_toolkit_transfer_attempt_summary"] = {
            "phase": phase,
            "committed_rows": committed_rows,
            "elapsed_seconds": elapsed_seconds,
        }
    except (AttributeError, TypeError):
        return
