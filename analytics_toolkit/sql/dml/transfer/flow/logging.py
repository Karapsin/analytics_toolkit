from __future__ import annotations

# ruff: noqa: PLR0913
import math
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from analytics_toolkit.sql.connection.errors import SqlOperationContext, sql_preview

if TYPE_CHECKING:
    from analytics_toolkit.sql.dml.transfer.runtime.models import (
        TransferOptions,
        TransferSlice,
    )

T = TypeVar("T")


class TransferAttemptLogState:
    def __init__(self) -> None:
        self.previous_summary: dict[str, object] | None = None

    def run(self, operation: Callable[[], T]) -> T:
        try:
            return operation()
        except BaseException as exc:
            summary = getattr(exc, "analytics_toolkit_transfer_attempt_summary", None)
            self.previous_summary = summary if isinstance(summary, dict) else None
            raise

    def retry_message(self, attempt: int, total_attempts: int) -> str:
        return transfer_retry_message(attempt, total_attempts, self.previous_summary)


class ProgressTracker:
    def __init__(self, progress_bar: Any) -> None:
        self.progress_bar = progress_bar
        self.total_rows = 0
        self._batch_start_rows = 0

    def start_batch(self) -> None:
        self._batch_start_rows = self.total_rows

    def update(self, rows: int) -> None:
        self.total_rows += rows
        self.progress_bar.update(rows)

    def complete_batch(self, rows: int) -> None:
        batch_progress_rows = self.total_rows - self._batch_start_rows
        remaining_rows = rows - batch_progress_rows
        if remaining_rows > 0:
            self.update(remaining_rows)


def build_transfer_operation_context(
    options: TransferOptions,
    attempt: int,
) -> SqlOperationContext:
    hide_source_sql = (
        options.transfer_slices is not None and options.source_transfer_staging_schema is not None
    )
    return SqlOperationContext(
        operation="transfer_table",
        alias=options.to_db_key,
        backend=options.to_db_backend,
        phase="transfer",
        target_table=options.target_table,
        retry_attempt=attempt,
        sql_preview=sql_preview(None if hide_source_sql else options.source_sql),
    )


def transfer_retry_message(
    attempt: int,
    total_attempts: int,
    summary: dict[str, object] | None,
) -> str:
    reset = "rows/second, memory/second, load ETA, and total transfer ETA reset"
    if summary is None:
        return f"Restarting transfer attempt {attempt}/{total_attempts}; {reset}"
    allowed_phases = {
        "metadata inspection",
        "source-stage loading",
        "aggregate stage validation",
        "target-stage consolidation",
        "destination validation",
        "destination finalization",
    }
    phase = summary.get("phase")
    safe_phase = phase if phase in allowed_phases else "transfer failure"
    committed_rows = summary.get("committed_rows")
    safe_rows = committed_rows if type(committed_rows) is int and committed_rows >= 0 else 0
    elapsed = summary.get("elapsed_seconds")
    safe_elapsed = (
        float(elapsed)
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
        else 0.0
    )
    if not math.isfinite(safe_elapsed) or safe_elapsed < 0:
        safe_elapsed = 0.0
    return (
        f"Restarting transfer attempt {attempt}/{total_attempts} after {safe_phase}: "
        f"previous attempt staged {safe_rows:,} rows in {safe_elapsed:.1f} seconds; {reset}"
    )


def format_transfer_slice_log_label(
    options: TransferOptions,
    transfer_slice: TransferSlice,
) -> str | None:
    transfer_keys = options.transfer_keys
    if not transfer_keys:
        return None
    parts = [
        f"{key}={format_transfer_slice_log_value(value)}"
        for key, value in zip(transfer_keys, transfer_slice.values)
    ]
    if not parts:
        return None
    return ", ".join(parts)


def format_transfer_slice_log_value(value: object) -> str:
    if value is None:
        return "NULL"
    return repr(value)


def format_transfer_key_log_fragment(label: str | None) -> str:
    if label is None:
        return ""
    return f"for {label} "


def pipeline_start_message(
    slice_count: int,
    read_workers: int,
    write_workers: int,
) -> str:
    return (
        f"Starting keyed transfer pipeline: {slice_count} slice(s), "
        f"{read_workers} read worker(s), {write_workers} target writer(s), "
        f"queue capacity {write_workers}"
    )


def staged_pipeline_start_message(
    slice_count: int,
    read_workers: int,
    write_workers: int,
) -> str:
    return (
        f"Starting keyed source-staging pipeline: {slice_count} slice(s), "
        f"{read_workers} source-stage reader(s), {write_workers} target writer(s); "
        "ready keys flow directly to target writers with no global stage barrier"
    )


def staged_reader_slice_message(
    worker_index: int,
    worker_count: int,
    slice_position: int,
    slice_count: int,
    key_label: str | None,
    action: str,
    stage_table: str,
) -> str:
    suffix = f" for {key_label}" if key_label else ""
    return (
        f"Source-stage reader {worker_index + 1}/{worker_count} {action} "
        f"slice {slice_position}/{slice_count}{suffix} {stage_table}"
    )


def staged_writer_key_message(
    worker_index: int,
    worker_count: int,
    slice_index: int,
    slice_count: int,
    key_label: str | None,
    action: str,
    stage_table: str,
) -> str:
    suffix = f" for {key_label}" if key_label else ""
    return (
        f"Target writer {worker_index + 1}/{worker_count} {action} whole slice "
        f"{slice_index + 1}/{slice_count}{suffix} {stage_table}"
    )


def reader_slice_message(
    worker_index: int,
    worker_count: int,
    slice_position: int,
    slice_count: int,
    key_label: str | None,
    action: str,
) -> str:
    suffix = f" for {key_label}" if key_label else ""
    return (
        f"Read worker {worker_index + 1}/{worker_count} {action} "
        f"slice {slice_position}/{slice_count}{suffix}"
    )


def writer_batch_message(
    worker_index: int,
    worker_count: int,
    batch_index: int,
    slice_position: int,
    slice_count: int,
    stage_table: str,
) -> str:
    return (
        f"Target writer {worker_index + 1}/{worker_count} staging batch {batch_index} "
        f"for slice {slice_position}/{slice_count} into {stage_table}"
    )


def pipeline_phase_message(phase: str, *, complete: bool = False) -> str:
    prefix = "Completed" if complete else "Starting"
    return f"{prefix} keyed transfer pipeline {phase}"
