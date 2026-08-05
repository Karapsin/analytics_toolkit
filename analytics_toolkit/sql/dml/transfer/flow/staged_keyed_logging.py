from __future__ import annotations

from typing import TYPE_CHECKING

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.dml.transfer.flow.transfer_progress import (
    format_duration,
    format_eta,
    format_memory_rate,
    format_rows_rate,
    format_slice_tag,
)

if TYPE_CHECKING:
    from analytics_toolkit.sql.dml.transfer.flow.lazy_keyed_runtime import (
        LazyKeyedRuntime,
        ReadyKeyTask,
    )
    from analytics_toolkit.sql.dml.transfer.flow.transfer_progress import (
        BatchProgress,
        KeyVerificationProgress,
        TransferProgressSnapshot,
    )
    from analytics_toolkit.sql.dml.transfer.runtime.models import (
        TransferOptions,
        TransferSlice,
    )


def slice_tag(options: TransferOptions, transfer_slice: TransferSlice) -> str:
    slices = options.transfer_slices or []
    position = transfer_slice.index + 1
    if not 1 <= position <= len(slices):
        message = "Transfer slice index is outside the normalized slice sequence."
        raise ValueError(message)
    key_values = (
        list(zip(options.transfer_keys, transfer_slice.values)) if options.transfer_keys else None
    )
    return format_slice_tag(position, len(slices), key_values)


def log_pipeline_start(options: TransferOptions, runtime: LazyKeyedRuntime) -> None:
    concurrency = options.transfer_concurrency
    soft_cap = (
        "none"
        if concurrency.soft_concurrency_cap is None
        else str(concurrency.soft_concurrency_cap)
    )
    time_print(
        f"Starting keyed source-stage transfer: {len(options.transfer_slices or []):,} keys; "
        f"requested readers/writers {concurrency.requested_read}/{concurrency.requested_write}; "
        f"soft cap {soft_cap}; hard cap {concurrency.hard_concurrency_cap}; effective "
        f"readers/writers {concurrency.effective_read}/{concurrency.effective_write}; source "
        f"connection limit {concurrency.effective_read}; target connection limit "
        f"{concurrency.effective_write}; prefetch 1 batch/writer; live source-stage limit "
        f"{runtime.live_stage_limit}"
    )


def log_batch_progress(task: ReadyKeyTask, progress: BatchProgress) -> None:
    snapshot = progress.snapshot
    estimated = snapshot.estimated_total_rows
    estimate_prefix = "~" if snapshot.total_rows_are_approximate else ""
    total_text = "?" if estimated is None else f"{estimate_prefix}{estimated:,}"
    time_print(
        f"{task.tag} Staged batch {progress.batch_index}: {progress.batch_rows:,} rows; "
        f"key {progress.key_committed_rows:,}/{task.expected_rows:,}; total "
        f"{snapshot.committed_rows:,}/{total_text}; batch time "
        f"{format_duration(progress.timing.batch_seconds)}; total time "
        f"{format_duration(snapshot.attempt_elapsed_seconds)}; batch rate "
        f"{format_rows_rate(progress.batch_rows_per_second)}; rolling rate "
        f"{format_rows_rate(snapshot.rolling_rows_per_second)}; approximate RAM rate "
        f"{format_memory_rate(progress.batch_memory_bytes_per_second)}; rolling approximate "
        "RAM rate "
        f"{format_memory_rate(snapshot.rolling_memory_bytes_per_second)}; load ETA "
        f"{format_eta(snapshot.load_eta_seconds, approximate=snapshot.unknown_key_count > 0)}; "
        "total transfer ETA "
        f"{format_eta(snapshot.total_transfer_eta_seconds, approximate=True)}"
    )


def log_key_verification(
    task: ReadyKeyTask,
    progress: KeyVerificationProgress,
) -> None:
    snapshot = progress.snapshot
    estimated = snapshot.estimated_total_rows
    estimate_prefix = "~" if snapshot.total_rows_are_approximate else ""
    total_text = "?" if estimated is None else f"{estimate_prefix}{estimated:,}"
    time_print(
        f"{task.tag} Verified {progress.expected_rows:,} rows; key time "
        f"{format_duration(progress.key_elapsed_seconds)}; total {snapshot.committed_rows:,}/"
        f"{total_text}; total time {format_duration(snapshot.attempt_elapsed_seconds)}; "
        f"rolling rate {format_rows_rate(snapshot.rolling_rows_per_second)}; rolling "
        f"approximate RAM rate {format_memory_rate(snapshot.rolling_memory_bytes_per_second)}; "
        "load ETA "
        f"{format_eta(snapshot.load_eta_seconds, approximate=snapshot.unknown_key_count > 0)}; "
        "total transfer ETA "
        f"{format_eta(snapshot.total_transfer_eta_seconds, approximate=True)}"
    )


def log_loading_complete(snapshot: TransferProgressSnapshot) -> None:
    time_print(
        f"Completed source-stage loading: {snapshot.committed_rows:,} rows in "
        f"{format_duration(snapshot.attempt_elapsed_seconds)}; average rate "
        f"{format_rows_rate(snapshot.average_rows_per_second)}; average approximate RAM rate "
        f"{format_memory_rate(snapshot.average_memory_bytes_per_second)}; remaining total "
        f"transfer ETA {format_eta(snapshot.total_transfer_eta_seconds, approximate=True)}"
    )


def log_transfer_complete(
    options: TransferOptions,
    snapshot: TransferProgressSnapshot,
    runtime: LazyKeyedRuntime,
    elapsed: float,
) -> None:
    if snapshot.committed_rows == 0:
        time_print(
            f"Completed transfer: 0 rows from {options.from_db_key} to {options.to_db_key} in "
            f"{format_duration(elapsed)}; no batch throughput; load ETA 0 seconds; total "
            "transfer ETA 0 seconds"
        )
        return
    target_stage_count = len(runtime.target_stages)
    time_print(
        f"Completed transfer: {snapshot.committed_rows:,} rows from {options.from_db_key} to "
        f"{options.to_db_key} in {format_duration(elapsed)}; average staged rate "
        f"{format_rows_rate(snapshot.average_rows_per_second)}; average approximate RAM rate "
        f"{format_memory_rate(snapshot.average_memory_bytes_per_second)}; source stages dropped "
        f"{runtime.source_stages_dropped}/{len(options.transfer_slices or [])}; target stages "
        f"cleaned {target_stage_count}/{target_stage_count}"
    )


def attach_attempt_summary(
    error: BaseException,
    snapshot: TransferProgressSnapshot,
    phase: str,
) -> None:
    try:
        error.__dict__["analytics_toolkit_transfer_attempt_summary"] = {
            "phase": phase,
            "committed_rows": snapshot.committed_rows,
            "elapsed_seconds": snapshot.attempt_elapsed_seconds,
        }
    except (AttributeError, TypeError):
        return
