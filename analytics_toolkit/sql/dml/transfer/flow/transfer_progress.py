from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from analytics_toolkit.sql.dml.transfer.flow import (
    transfer_progress_formatting as _progress_formatting,
)

if TYPE_CHECKING:
    from collections.abc import Hashable


DEFAULT_ROLLING_WINDOW_SECONDS = 60.0
format_slice_tag = _progress_formatting.format_slice_tag
MIN_ETA_BATCH_COUNT = 2
_IEC_UNIT_BASE = 1024.0
_SUBTEN_SECONDS = 10
_SECONDS_PER_MINUTE = 60


class LogicalBatchConflictError(RuntimeError):
    """Raised when one logical batch ID is reused for different data."""


@dataclass(frozen=True)
class BatchTiming:
    """End-to-end timestamps captured for one successfully inserted batch."""

    read_started_at: float
    read_completed_at: float
    queued_at: float
    insert_completed_at: float
    approximate_memory_bytes: int

    def __post_init__(self) -> None:
        timestamps = (
            self.read_started_at,
            self.read_completed_at,
            self.queued_at,
            self.insert_completed_at,
        )
        if not all(math.isfinite(value) for value in timestamps):
            msg = "Batch timing values must be finite"
            raise ValueError(msg)
        if timestamps != tuple(sorted(timestamps)):
            msg = "Batch timing must follow read, queue, and insert order"
            raise ValueError(msg)
        _require_nonnegative_int(
            self.approximate_memory_bytes,
            name="approximate_memory_bytes",
        )

    @property
    def batch_seconds(self) -> float:
        """Time from source read start through successful target insert."""
        return self.insert_completed_at - self.read_started_at


@dataclass(frozen=True)
class TransferProgressSnapshot:
    attempt_number: int
    attempt_elapsed_seconds: float
    committed_rows: int
    approximate_committed_memory_bytes: int
    successful_batch_count: int
    verified_key_count: int
    total_key_count: int
    known_key_count: int
    unknown_key_count: int
    exact_known_rows: int
    estimated_total_rows: int | None
    total_rows_are_approximate: bool
    average_rows_per_second: float | None
    rolling_rows_per_second: float | None
    average_memory_bytes_per_second: float | None
    rolling_memory_bytes_per_second: float | None
    eta_rows_per_second: float | None
    remaining_load_rows: int | None
    remaining_consolidation_rows: int | None
    remaining_finalization_rows: int | None
    load_eta_seconds: float | None
    total_transfer_eta_seconds: float | None
    loading_complete: bool
    consolidation_complete: bool
    finalization_started: bool
    finalization_complete: bool


@dataclass(frozen=True)
class BatchProgress:
    logical_batch_id: Hashable
    key_id: Hashable
    batch_index: int
    batch_rows: int
    key_committed_rows: int
    key_expected_rows: int | None
    batch_rows_per_second: float | None
    batch_memory_bytes_per_second: float | None
    timing: BatchTiming
    snapshot: TransferProgressSnapshot


@dataclass(frozen=True)
class KeyVerificationProgress:
    key_id: Hashable
    expected_rows: int
    key_elapsed_seconds: float
    snapshot: TransferProgressSnapshot


@dataclass(frozen=True)
class _BatchFingerprint:
    key_id: Hashable
    batch_index: int
    rows: int
    approximate_memory_bytes: int


@dataclass(frozen=True)
class _RateSample:
    completed_at: float
    rows: int
    approximate_memory_bytes: int


class TransferProgressTracker:
    """Thread-safe, attempt-scoped transfer throughput and ETA state."""

    _attempt_started_at: float
    _loading_started_at: float | None
    _last_commit_at: float
    _committed_rows: int
    _committed_memory_bytes: int
    _exact_known_rows: int
    _assigned_expected_rows: int
    _non_primary_assigned_expected_rows: int
    _non_primary_assignment_count: int
    _key_started_at: dict[Hashable, float]
    _key_expected_rows: dict[Hashable, int]
    _key_committed_rows: dict[Hashable, int]
    _verified_keys: set[Hashable]
    _successful_batches: dict[Hashable, _BatchFingerprint]
    _writer_assignments: dict[Hashable, Hashable]
    _writer_stage_rows: dict[Hashable, int]
    _rate_samples: deque[_RateSample]
    _consolidation_operations: dict[Hashable, int]
    _consolidated_rows: int
    _loading_complete: bool
    _consolidation_complete: bool
    _finalization_started: bool
    _finalization_complete: bool

    def __init__(  # noqa: PLR0913
        self,
        *,
        total_key_count: int,
        active_writers: int = 1,
        rolling_window_seconds: float = DEFAULT_ROLLING_WINDOW_SECONDS,
        primary_writer_id: Hashable = 0,
        consolidation_enabled: bool = True,
        allow_split_key_writers: bool = False,
        attempt_number: int = 1,
        clock: Callable[[], float] = time.monotonic,
        progress_bar: Any = None,
    ) -> None:
        _require_nonnegative_int(total_key_count, name="total_key_count")
        _require_positive_int(active_writers, name="active_writers")
        _require_positive_int(attempt_number, name="attempt_number")
        if not math.isfinite(rolling_window_seconds) or rolling_window_seconds <= 0:
            msg = "rolling_window_seconds must be a finite positive number"
            raise ValueError(msg)
        _ensure_hashable(primary_writer_id, name="primary_writer_id")
        if not isinstance(consolidation_enabled, bool):
            msg = "consolidation_enabled must be a boolean"
            raise ValueError(msg)  # noqa: TRY004
        if not isinstance(allow_split_key_writers, bool):
            msg = "allow_split_key_writers must be a boolean"
            raise ValueError(msg)  # noqa: TRY004
        self._total_key_count = total_key_count
        self._active_writers = active_writers
        self._rolling_window_seconds = float(rolling_window_seconds)
        self._primary_writer_id = primary_writer_id
        self._consolidation_enabled = consolidation_enabled
        self._allow_split_key_writers = allow_split_key_writers
        self._clock = clock
        self._progress_bar = progress_bar
        self._lock = threading.RLock()
        self._attempt_number = attempt_number
        with self._lock:
            self._reset_locked(float(self._clock()))

    def reset(
        self,
        *,
        attempt_number: int | None = None,
        started_at: float | None = None,
    ) -> None:
        with self._lock:
            if attempt_number is None:
                self._attempt_number += 1
            else:
                _require_positive_int(attempt_number, name="attempt_number")
                self._attempt_number = attempt_number
            reset_at = float(self._clock()) if started_at is None else started_at
            if not math.isfinite(reset_at):
                msg = "started_at must be finite"
                raise ValueError(msg)
            self._reset_locked(reset_at)
            reset_progress = getattr(self._progress_bar, "reset", None)
            if callable(reset_progress):
                reset_progress()

    def start_key(self, key_id: Hashable, *, started_at: float | None = None) -> bool:
        _ensure_hashable(key_id, name="key_id")
        with self._lock:
            if key_id in self._key_started_at:
                return False
            value = float(self._clock()) if started_at is None else started_at
            if not math.isfinite(value):
                msg = "started_at must be finite"
                raise ValueError(msg)
            self._key_started_at[key_id] = max(value, self._attempt_started_at)
            return True

    def materialize_key(
        self,
        key_id: Hashable,
        expected_rows: int,
        *,
        started_at: float | None = None,
    ) -> bool:
        _ensure_hashable(key_id, name="key_id")
        _require_nonnegative_int(expected_rows, name="expected_rows")
        with self._lock:
            existing = self._key_expected_rows.get(key_id)
            if existing is not None:
                if existing != expected_rows:
                    msg = "A materialized key cannot change its expected row count"
                    raise ValueError(msg)
                return False
            if len(self._key_expected_rows) >= self._total_key_count:
                msg = "Materialized key count exceeds total_key_count"
                raise ValueError(msg)
            if self._key_committed_rows.get(key_id, 0) > expected_rows:
                msg = "Materialized row count is below rows already committed for the key"
                raise ValueError(msg)
            self._key_expected_rows[key_id] = expected_rows
            self._exact_known_rows += expected_rows
            if key_id in self._writer_assignments:
                assigned_writer = self._writer_assignments[key_id]
                self._assigned_expected_rows += expected_rows
                if assigned_writer != self._primary_writer_id:
                    self._non_primary_assigned_expected_rows += expected_rows
            if key_id not in self._key_started_at:
                value = float(self._clock()) if started_at is None else started_at
                if not math.isfinite(value):
                    msg = "started_at must be finite"
                    raise ValueError(msg)
                self._key_started_at[key_id] = max(value, self._attempt_started_at)
            return True

    def assign_key(self, key_id: Hashable, writer_id: Hashable) -> bool:
        _ensure_hashable(key_id, name="key_id")
        _ensure_hashable(writer_id, name="writer_id")
        with self._lock:
            existing = self._writer_assignments.get(key_id)
            if existing is not None:
                if existing != writer_id:
                    msg = "A transfer key cannot move between target writers"
                    raise ValueError(msg)
                return False
            if len(self._writer_assignments) >= self._total_key_count:
                msg = "Assigned key count exceeds total_key_count"
                raise ValueError(msg)
            self._writer_assignments[key_id] = writer_id
            if writer_id != self._primary_writer_id:
                self._non_primary_assignment_count += 1
            expected_rows = self._key_expected_rows.get(key_id)
            if expected_rows is not None:
                self._assigned_expected_rows += expected_rows
                if writer_id != self._primary_writer_id:
                    self._non_primary_assigned_expected_rows += expected_rows
            return True

    def set_primary_writer(self, writer_id: Hashable) -> None:
        """Update the stage that will receive consolidation and recompute estimates."""
        _ensure_hashable(writer_id, name="writer_id")
        with self._lock:
            if writer_id == self._primary_writer_id:
                return
            self._primary_writer_id = writer_id
            self._assigned_expected_rows = 0
            self._non_primary_assigned_expected_rows = 0
            self._non_primary_assignment_count = 0
            for key_id, assigned_writer in self._writer_assignments.items():
                expected_rows = self._key_expected_rows.get(key_id)
                if expected_rows is not None:
                    self._assigned_expected_rows += expected_rows
                if assigned_writer != writer_id:
                    self._non_primary_assignment_count += 1
                    if expected_rows is not None:
                        self._non_primary_assigned_expected_rows += expected_rows

    def commit_batch(  # noqa: PLR0913
        self,
        *,
        logical_batch_id: Hashable,
        key_id: Hashable,
        batch_index: int,
        rows: int,
        timing: BatchTiming,
        writer_id: Hashable | None = None,
    ) -> BatchProgress | None:
        """Record one target-stage commit, idempotently by logical batch ID."""
        _ensure_hashable(logical_batch_id, name="logical_batch_id")
        _ensure_hashable(key_id, name="key_id")
        _require_positive_int(batch_index, name="batch_index")
        _require_positive_int(rows, name="rows")
        if writer_id is not None:
            _ensure_hashable(writer_id, name="writer_id")
        fingerprint = _BatchFingerprint(
            key_id=key_id,
            batch_index=batch_index,
            rows=rows,
            approximate_memory_bytes=timing.approximate_memory_bytes,
        )
        with self._lock:
            existing = self._successful_batches.get(logical_batch_id)
            if existing is not None:
                if existing != fingerprint:
                    msg = "A successful logical batch ID was reused for different data"
                    raise LogicalBatchConflictError(msg)
                return None
            if self._loading_complete:
                msg = "Cannot commit a batch after loading is complete"
                raise RuntimeError(msg)
            if key_id not in self._key_expected_rows:
                msg = "Cannot commit a batch before its source key count is known"
                raise RuntimeError(msg)
            expected_rows = self._key_expected_rows[key_id]
            key_rows = self._key_committed_rows.get(key_id, 0) + rows
            if key_rows > expected_rows:
                msg = "Committed key rows exceed its materialized source count"
                raise ValueError(msg)
            resolved_writer = self._resolve_writer_locked(key_id, writer_id)
            self._successful_batches[logical_batch_id] = fingerprint
            self._key_committed_rows[key_id] = key_rows
            self._committed_rows += rows
            self._committed_memory_bytes += timing.approximate_memory_bytes
            self._record_loading_start_locked(timing.read_started_at)
            if resolved_writer is not None:
                self._writer_stage_rows[resolved_writer] = (
                    self._writer_stage_rows.get(resolved_writer, 0) + rows
                )
            completed_at = max(timing.insert_completed_at, self._last_commit_at)
            self._last_commit_at = completed_at
            self._rate_samples.append(
                _RateSample(
                    completed_at=completed_at,
                    rows=self._committed_rows,
                    approximate_memory_bytes=self._committed_memory_bytes,
                )
            )
            snapshot = self._snapshot_locked(completed_at)
            batch_seconds = timing.batch_seconds
            progress = BatchProgress(
                logical_batch_id=logical_batch_id,
                key_id=key_id,
                batch_index=batch_index,
                batch_rows=rows,
                key_committed_rows=key_rows,
                key_expected_rows=expected_rows,
                batch_rows_per_second=(rows / batch_seconds if batch_seconds > 0 else None),
                batch_memory_bytes_per_second=(
                    timing.approximate_memory_bytes / batch_seconds if batch_seconds > 0 else None
                ),
                timing=timing,
                snapshot=snapshot,
            )
            if self._progress_bar is not None:
                self._progress_bar.update(rows)
        return progress

    def verify_key(
        self,
        key_id: Hashable,
        *,
        verified_at: float | None = None,
    ) -> KeyVerificationProgress | None:
        """Mark a key verified only when its committed row count is exact."""
        _ensure_hashable(key_id, name="key_id")
        with self._lock:
            if key_id in self._verified_keys:
                return None
            if key_id not in self._key_expected_rows:
                msg = "Cannot verify a key before its source count is known"
                raise RuntimeError(msg)
            expected_rows = self._key_expected_rows[key_id]
            if self._key_committed_rows.get(key_id, 0) != expected_rows:
                msg = "Cannot verify a key before exactly all rows are committed"
                raise RuntimeError(msg)
            value = float(self._clock()) if verified_at is None else verified_at
            if not math.isfinite(value):
                msg = "verified_at must be finite"
                raise ValueError(msg)
            completed_at = max(value, self._last_commit_at)
            self._verified_keys.add(key_id)
            key_started_at = self._key_started_at.get(
                key_id,
                self._attempt_started_at,
            )
            return KeyVerificationProgress(
                key_id=key_id,
                expected_rows=expected_rows,
                key_elapsed_seconds=max(completed_at - key_started_at, 0.0),
                snapshot=self._snapshot_locked(completed_at),
            )

    def mark_loading_complete(self) -> TransferProgressSnapshot:
        with self._lock:
            if len(self._key_expected_rows) != self._total_key_count:
                msg = "Loading cannot complete while source key counts are unknown"
                raise RuntimeError(msg)
            if self._committed_rows != self._exact_known_rows:
                msg = "Loading cannot complete before every source row is committed"
                raise RuntimeError(msg)
            self._loading_complete = True
            return self._snapshot_locked(self._now_locked())

    def record_consolidated_rows(
        self,
        *,
        logical_operation_id: Hashable,
        rows: int,
    ) -> bool:
        """Record committed consolidation work without double-counting retries."""
        _ensure_hashable(logical_operation_id, name="logical_operation_id")
        _require_nonnegative_int(rows, name="rows")
        with self._lock:
            existing = self._consolidation_operations.get(logical_operation_id)
            if existing is not None:
                if existing != rows:
                    msg = "A consolidation operation ID was reused for different rows"
                    raise LogicalBatchConflictError(msg)
                return False
            if self._consolidation_complete:
                msg = "Cannot record consolidation after it is complete"
                raise RuntimeError(msg)
            self._consolidation_operations[logical_operation_id] = rows
            self._consolidated_rows += rows
            return True

    def mark_consolidation_complete(self) -> TransferProgressSnapshot:
        with self._lock:
            if not self._loading_complete:
                msg = "Consolidation cannot complete before loading"
                raise RuntimeError(msg)
            self._consolidation_complete = True
            return self._snapshot_locked(self._now_locked())

    def mark_finalization_started(self) -> TransferProgressSnapshot:
        with self._lock:
            if not self._consolidation_complete:
                msg = "Finalization cannot start before consolidation"
                raise RuntimeError(msg)
            self._finalization_started = True
            return self._snapshot_locked(self._now_locked())

    def mark_finalization_complete(self) -> TransferProgressSnapshot:
        with self._lock:
            if not self._finalization_started:
                msg = "Finalization cannot complete before it starts"
                raise RuntimeError(msg)
            self._finalization_complete = True
            return self._snapshot_locked(self._now_locked())

    def snapshot(self, *, at: float | None = None) -> TransferProgressSnapshot:
        with self._lock:
            value = float(self._clock()) if at is None else at
            if not math.isfinite(value):
                msg = "Snapshot time must be finite"
                raise ValueError(msg)
            return self._snapshot_locked(max(value, self._last_commit_at))

    def _reset_locked(self, started_at: float) -> None:
        self._attempt_started_at = started_at
        self._loading_started_at = None
        self._last_commit_at = started_at
        self._committed_rows = 0
        self._committed_memory_bytes = 0
        self._exact_known_rows = 0
        self._assigned_expected_rows = 0
        self._non_primary_assigned_expected_rows = 0
        self._non_primary_assignment_count = 0
        self._key_started_at = {}
        self._key_expected_rows = {}
        self._key_committed_rows = {}
        self._verified_keys = set()
        self._successful_batches = {}
        self._writer_assignments = {}
        self._writer_stage_rows = {}
        self._rate_samples = deque()
        self._consolidation_operations = {}
        self._consolidated_rows = 0
        self._loading_complete = False
        self._consolidation_complete = False
        self._finalization_started = False
        self._finalization_complete = False

    def _resolve_writer_locked(
        self,
        key_id: Hashable,
        writer_id: Hashable | None,
    ) -> Hashable | None:
        if self._allow_split_key_writers:
            return writer_id
        assigned_writer = self._writer_assignments.get(key_id)
        if assigned_writer is not None and writer_id is not None and assigned_writer != writer_id:
            msg = "Committed writer does not own the whole transfer key"
            raise ValueError(msg)
        if assigned_writer is not None:
            return assigned_writer
        if writer_id is not None:
            if len(self._writer_assignments) >= self._total_key_count:
                msg = "Assigned key count exceeds total_key_count"
                raise ValueError(msg)
            self._writer_assignments[key_id] = writer_id
            if writer_id != self._primary_writer_id:
                self._non_primary_assignment_count += 1
                self._non_primary_assigned_expected_rows += self._key_expected_rows[key_id]
            self._assigned_expected_rows += self._key_expected_rows[key_id]
        return writer_id

    def _now_locked(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            msg = "Clock value must be finite"
            raise ValueError(msg)
        return max(value, self._last_commit_at)

    def _record_loading_start_locked(self, read_started_at: float) -> None:
        started_at = max(read_started_at, self._attempt_started_at)
        if self._loading_started_at is not None and started_at >= self._loading_started_at:
            return
        self._loading_started_at = started_at
        anchor = _RateSample(started_at, 0, 0)
        if self._rate_samples and self._rate_samples[0].rows == 0:
            self._rate_samples[0] = anchor
        else:
            self._rate_samples.appendleft(anchor)

    def _snapshot_locked(self, at: float) -> TransferProgressSnapshot:
        elapsed = max(at - self._attempt_started_at, 0.0)
        all_loading_rows_committed = (
            len(self._key_expected_rows) == self._total_key_count
            and self._committed_rows == self._exact_known_rows
        )
        rate_at = (
            self._last_commit_at if self._loading_complete or all_loading_rows_committed else at
        )
        loading_elapsed = (
            max(rate_at - self._loading_started_at, 0.0)
            if self._loading_started_at is not None
            else 0.0
        )
        average_rows_rate = _rate(self._committed_rows, loading_elapsed)
        average_memory_rate = _rate(self._committed_memory_bytes, loading_elapsed)
        rolling_rows_rate, rolling_memory_rate = self._rolling_rates_locked(rate_at)
        estimated_rows, approximate = self._estimated_total_rows_locked()
        remaining_load = self._remaining_load_rows_locked(estimated_rows)
        remaining_consolidation = self._remaining_consolidation_rows_locked(estimated_rows)
        remaining_finalization = self._remaining_finalization_rows_locked(estimated_rows)
        eta_rate = self._eta_rate_locked(average_rows_rate, rolling_rows_rate)
        load_eta = _eta_seconds(remaining_load, eta_rate)
        total_remaining = _sum_optional_rows(
            remaining_load,
            remaining_consolidation,
            remaining_finalization,
        )
        total_eta = _eta_seconds(total_remaining, eta_rate)
        known_count = len(self._key_expected_rows)
        if known_count == self._total_key_count and estimated_rows == 0:
            load_eta = 0.0
            total_eta = 0.0
        return TransferProgressSnapshot(
            attempt_number=self._attempt_number,
            attempt_elapsed_seconds=elapsed,
            committed_rows=self._committed_rows,
            approximate_committed_memory_bytes=self._committed_memory_bytes,
            successful_batch_count=len(self._successful_batches),
            verified_key_count=len(self._verified_keys),
            total_key_count=self._total_key_count,
            known_key_count=known_count,
            unknown_key_count=self._total_key_count - known_count,
            exact_known_rows=self._exact_known_rows,
            estimated_total_rows=estimated_rows,
            total_rows_are_approximate=approximate,
            average_rows_per_second=average_rows_rate,
            rolling_rows_per_second=rolling_rows_rate,
            average_memory_bytes_per_second=average_memory_rate,
            rolling_memory_bytes_per_second=rolling_memory_rate,
            eta_rows_per_second=eta_rate,
            remaining_load_rows=remaining_load,
            remaining_consolidation_rows=remaining_consolidation,
            remaining_finalization_rows=remaining_finalization,
            load_eta_seconds=load_eta,
            total_transfer_eta_seconds=total_eta,
            loading_complete=self._loading_complete,
            consolidation_complete=self._consolidation_complete,
            finalization_started=self._finalization_started,
            finalization_complete=self._finalization_complete,
        )

    def _rolling_rates_locked(self, at: float) -> tuple[float | None, float | None]:
        cutoff = at - self._rolling_window_seconds
        while len(self._rate_samples) > 1:
            second_sample = self._rate_samples[1]
            if second_sample.completed_at > cutoff:
                break
            self._rate_samples.popleft()
        if len(self._successful_batches) < MIN_ETA_BATCH_COUNT:
            return None, None
        baseline = self._rate_samples[0]
        baseline_at = max(baseline.completed_at, cutoff)
        duration = at - baseline_at
        if duration <= 0:
            return None, None
        row_delta = self._committed_rows - baseline.rows
        memory_delta = self._committed_memory_bytes - baseline.approximate_memory_bytes
        return _rate(row_delta, duration), _rate(memory_delta, duration)

    def _estimated_total_rows_locked(self) -> tuple[int | None, bool]:
        known_count = len(self._key_expected_rows)
        unknown_count = self._total_key_count - known_count
        known_rows = self._exact_known_rows
        if unknown_count == 0:
            return known_rows, False
        if known_count == 0:
            return None, True
        estimated_rows = known_rows + known_rows * unknown_count / known_count
        return math.floor(estimated_rows + 0.5), True

    def _remaining_load_rows_locked(self, estimated_rows: int | None) -> int | None:
        if self._loading_complete:
            return 0
        if estimated_rows is None:
            return None
        return max(estimated_rows - self._committed_rows, 0)

    def _remaining_consolidation_rows_locked(
        self,
        estimated_rows: int | None,
    ) -> int | None:
        if self._consolidation_complete:
            return 0
        if not self._consolidation_enabled:
            return 0
        if estimated_rows is None:
            return None
        if self._loading_complete:
            expected_rows = sum(
                rows
                for writer_id, rows in self._writer_stage_rows.items()
                if writer_id != self._primary_writer_id
            )
        else:
            fraction = self._non_primary_assignment_fraction_locked()
            expected_rows = math.floor(estimated_rows * fraction + 0.5)
        return max(expected_rows - self._consolidated_rows, 0)

    def _non_primary_assignment_fraction_locked(self) -> float:
        if self._allow_split_key_writers and self._writer_stage_rows:
            assigned_rows = sum(self._writer_stage_rows.values())
            if assigned_rows > 0:
                non_primary_rows = sum(
                    rows
                    for writer_id, rows in self._writer_stage_rows.items()
                    if writer_id != self._primary_writer_id
                )
                return non_primary_rows / assigned_rows
        if not self._writer_assignments:
            return (self._active_writers - 1) / self._active_writers
        if self._assigned_expected_rows > 0:
            return self._non_primary_assigned_expected_rows / self._assigned_expected_rows
        return self._non_primary_assignment_count / len(self._writer_assignments)

    def _remaining_finalization_rows_locked(
        self,
        estimated_rows: int | None,
    ) -> int | None:
        if self._finalization_complete:
            return 0
        return estimated_rows

    def _eta_rate_locked(
        self,
        average_rows_rate: float | None,
        rolling_rows_rate: float | None,
    ) -> float | None:
        if len(self._successful_batches) < MIN_ETA_BATCH_COUNT:
            return None
        positive_rates = [
            value
            for value in (average_rows_rate, rolling_rows_rate)
            if value is not None and value > 0
        ]
        return min(positive_rates) if positive_rates else None


def format_memory_rate(bytes_per_second: float | None) -> str:
    """Format approximate in-process RAM throughput using IEC units."""
    if bytes_per_second is None:
        return "unavailable"
    if not math.isfinite(bytes_per_second) or bytes_per_second < 0:
        msg = "bytes_per_second must be finite and non-negative"
        raise ValueError(msg)
    units = ("B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s")
    value = bytes_per_second
    unit = units[0]
    for candidate in units[1:]:
        if value < _IEC_UNIT_BASE:
            break
        value /= _IEC_UNIT_BASE
        unit = candidate
    if unit == "B/s":
        return f"{value:.0f} {unit}"
    return f"{value:.1f} {unit}"


def format_rows_rate(rows_per_second: float | None) -> str:
    if rows_per_second is None:
        return "unavailable"
    if not math.isfinite(rows_per_second) or rows_per_second < 0:
        msg = "rows_per_second must be finite and non-negative"
        raise ValueError(msg)
    return f"{rows_per_second:,.0f} rows/s"


def format_eta(seconds: float | None, *, approximate: bool) -> str:
    if seconds is None:
        return "unavailable"
    prefix = "~" if approximate else ""
    return f"{prefix}{format_duration(seconds)}"


def format_duration(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        msg = "seconds must be finite and non-negative"
        raise ValueError(msg)
    if seconds < _SUBTEN_SECONDS and not float(seconds).is_integer():
        return f"{seconds:.1f} seconds"
    rounded_seconds = math.floor(seconds + 0.5)
    if rounded_seconds < _SECONDS_PER_MINUTE:
        unit = "second" if rounded_seconds == 1 else "seconds"
        return f"{rounded_seconds} {unit}"
    minutes, remaining_seconds = divmod(rounded_seconds, _SECONDS_PER_MINUTE)
    minute_unit = "minute" if minutes == 1 else "minutes"
    if remaining_seconds == 0:
        return f"{minutes} {minute_unit}"
    second_unit = "second" if remaining_seconds == 1 else "seconds"
    return f"{minutes} {minute_unit} {remaining_seconds} {second_unit}"


def _rate(value: int, duration: float) -> float | None:
    if duration <= 0:
        return None
    return value / duration


def _eta_seconds(rows: int | None, rate: float | None) -> float | None:
    if rows is None:
        return None
    if rate is None or rate <= 0:
        return None
    if rows == 0:
        return 0.0
    return rows / rate


def _sum_optional_rows(*values: int | None) -> int | None:
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _require_nonnegative_int(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        msg = f"{name} must be a built-in non-negative integer"
        raise ValueError(msg)


def _require_positive_int(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = f"{name} must be a built-in positive integer"
        raise ValueError(msg)


def _ensure_hashable(value: Hashable, *, name: str) -> None:
    try:
        hash(value)
    except TypeError as exc:
        msg = f"{name} must be hashable"
        raise TypeError(msg) from exc
