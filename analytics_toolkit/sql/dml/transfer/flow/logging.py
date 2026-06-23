from __future__ import annotations

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
