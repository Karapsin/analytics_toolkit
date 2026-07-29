from __future__ import annotations

# ruff: noqa: PLR0913
from typing import Any

from ..runtime.models import TransferOptions, TransferSlice


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
        "target writers start after all source stages complete"
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
