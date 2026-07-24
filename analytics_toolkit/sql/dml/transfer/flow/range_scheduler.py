from __future__ import annotations

# ruff: noqa: EM101, EM102, I001, TRY003

from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Mapping


@dataclass(frozen=True, order=True)
class OrdinalRange:
    slice_id: int
    start_ordinal: int
    stop_ordinal: int

    def __post_init__(self) -> None:
        if self.slice_id < 0:
            raise ValueError("slice_id must be non-negative.")
        if self.start_ordinal < 1:
            raise ValueError("start_ordinal must be at least one.")
        if self.stop_ordinal <= self.start_ordinal:
            raise ValueError("stop_ordinal must be greater than start_ordinal.")

    @property
    def row_count(self) -> int:
        return self.stop_ordinal - self.start_ordinal


class AdaptiveRangeScheduler:
    """Thread-safe half-open ordinal range claiming and checkpointing."""

    def __init__(self, slice_row_counts: Mapping[int, int]) -> None:
        normalized: dict[int, int] = {}
        for slice_id, row_count in slice_row_counts.items():
            if slice_id < 0 or row_count < 0:
                raise ValueError("slice IDs and row counts must be non-negative.")
            normalized[int(slice_id)] = int(row_count)
        self._expected = normalized
        self._pending: deque[OrdinalRange] = deque(
            OrdinalRange(slice_id, 1, row_count + 1)
            for slice_id, row_count in sorted(normalized.items())
            if row_count
        )
        self._claimed: dict[OrdinalRange, int] = {}
        self._completed: set[OrdinalRange] = set()
        self._lock = Lock()

    def claim(self, worker_id: int, batch_size: int) -> OrdinalRange | None:
        if worker_id < 0:
            raise ValueError("worker_id must be non-negative.")
        if batch_size < 1:
            raise ValueError("batch_size must be positive.")
        with self._lock:
            if not self._pending:
                return None
            pending = self._pending.popleft()
            stop = min(pending.stop_ordinal, pending.start_ordinal + batch_size)
            claimed = OrdinalRange(pending.slice_id, pending.start_ordinal, stop)
            if stop < pending.stop_ordinal:
                self._pending.appendleft(OrdinalRange(pending.slice_id, stop, pending.stop_ordinal))
            self._claimed[claimed] = worker_id
            return claimed

    def complete(self, worker_id: int, claimed: OrdinalRange) -> None:
        with self._lock:
            owner = self._claimed.get(claimed)
            if owner != worker_id:
                raise RuntimeError("Ordinal range was not claimed by this worker.")
            del self._claimed[claimed]
            self._completed.add(claimed)

    def requeue_failed(
        self,
        worker_id: int,
        claimed: OrdinalRange,
        *,
        reduced_batch_size: int,
    ) -> None:
        if reduced_batch_size < 1:
            raise ValueError("reduced_batch_size must be positive.")
        with self._lock:
            owner = self._claimed.get(claimed)
            if owner != worker_id:
                raise RuntimeError("Ordinal range was not claimed by this worker.")
            del self._claimed[claimed]
            children = [
                OrdinalRange(
                    claimed.slice_id,
                    start,
                    min(start + reduced_batch_size, claimed.stop_ordinal),
                )
                for start in range(
                    claimed.start_ordinal,
                    claimed.stop_ordinal,
                    reduced_batch_size,
                )
            ]
            self._pending.extendleft(reversed(children))

    @property
    def finished(self) -> bool:
        with self._lock:
            return not self._pending and not self._claimed

    def completed_ranges(self) -> tuple[OrdinalRange, ...]:
        with self._lock:
            return tuple(sorted(self._completed))

    def validate_complete(self) -> None:
        with self._lock:
            if self._pending or self._claimed:
                raise RuntimeError("Ordinal scheduler still has incomplete ranges.")
            by_slice: dict[int, list[OrdinalRange]] = {}
            for interval in self._completed:
                by_slice.setdefault(interval.slice_id, []).append(interval)
            for slice_id, expected_rows in self._expected.items():
                expected_start = 1
                for interval in sorted(by_slice.get(slice_id, [])):
                    if interval.start_ordinal != expected_start:
                        raise RuntimeError(
                            f"Ordinal coverage for slice {slice_id} has a gap or overlap."
                        )
                    expected_start = interval.stop_ordinal
                if expected_start != expected_rows + 1:
                    raise RuntimeError(f"Ordinal coverage for slice {slice_id} is incomplete.")
