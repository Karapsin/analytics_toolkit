from __future__ import annotations

import sys
from dataclasses import dataclass, field
from collections import deque
from typing import Any

import pandas as pd

DEFAULT_GP_INSERT_CHUNK_SIZE = 10_000


@dataclass(frozen=True)
class RowBatch:
    columns: list[str]
    rows: list[tuple[Any, ...]]

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def empty(self) -> bool:
        return self.row_count == 0

    def to_dataframe(self, *, include_rows: bool = False) -> pd.DataFrame:
        if include_rows:
            return pd.DataFrame(self.rows, columns=self.columns)
        return pd.DataFrame(columns=self.columns)

    def approx_memory_bytes(self) -> int:
        return _approx_sizeof(self.columns) + _approx_sizeof(self.rows)


def _approx_sizeof(
    value: Any,
    *,
    _seen: set[int] | None = None,
    _depth: int = 0,
    _max_depth: int = 8,
) -> int:
    if _seen is None:
        _seen = set()

    if _depth > _max_depth:
        return sys.getsizeof(value)

    value_id = id(value)
    if value_id in _seen:
        return 0
    _seen.add(value_id)

    size = sys.getsizeof(value)
    if isinstance(value, dict):
        for key, item in value.items():
            size += _approx_sizeof(
                key,
                _seen=_seen,
                _depth=_depth + 1,
                _max_depth=_max_depth,
            )
            size += _approx_sizeof(
                item,
                _seen=_seen,
                _depth=_depth + 1,
                _max_depth=_max_depth,
            )
        return size

    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            size += _approx_sizeof(
                item,
                _seen=_seen,
                _depth=_depth + 1,
                _max_depth=_max_depth,
            )
    return size


@dataclass
class AdaptiveBatchSizer:
    enabled: bool
    current_size: int
    min_size: int
    max_size: int | None
    target_seconds: float
    min_target_seconds: float | None = None
    max_target_seconds: float | None = None
    optimize_by_rows_per_second: bool = True
    target_rows_per_second_window: int = 5
    target_rows_per_second_deadband: float = 0.15
    adaptive_batch_size_step: float = 0.1
    rows_per_second_samples: deque[float] = field(default_factory=deque, init=False)
    previous_rows_per_second: float | None = None
    baseline_size: int | None = None
    baseline_rows_per_second: float | None = None
    probe_direction: str | None = None
    is_experimental_size: bool = False
    noop_probe_size: int | None = None
    noop_probe_direction: str | None = None
    target_memory_bytes: int | None = None
    min_target_memory_bytes: int | None = None
    max_target_memory_bytes: int | None = None

    def __post_init__(self) -> None:
        self.rows_per_second_samples = deque(
            maxlen=self.target_rows_per_second_window,
        )

    def update(
        self,
        duration_seconds: float,
        *,
        inserted_rows: int | None = None,
        memory_bytes: int | None = None,
    ) -> None:
        if not self.enabled:
            return

        if self.target_memory_bytes is not None:
            if memory_bytes is None:
                return
            self._update_for_memory(memory_bytes)
            return

        if self.optimize_by_rows_per_second:
            if inserted_rows is None or inserted_rows <= 0:
                return
            self._update_for_rows_per_second(duration_seconds, inserted_rows)
            return

        target_seconds = self._resolve_target_seconds()
        if duration_seconds < target_seconds / 2:
            grown_size = max(self.current_size + 1, (self.current_size * 3 + 1) // 2)
            self.current_size = self._cap_size(grown_size)
            return

        if duration_seconds > target_seconds * 2:
            shrunk_size = max(1, int(self.current_size * 0.5))
            self.current_size = max(shrunk_size, self.min_size)

    def _update_for_rows_per_second(
        self,
        duration_seconds: float,
        inserted_rows: int,
    ) -> None:
        if duration_seconds <= 0:
            return

        rows_per_second = inserted_rows / duration_seconds
        self.rows_per_second_samples.append(rows_per_second)
        previous_rows_per_second = self.previous_rows_per_second
        smoothed_rows_per_second = (
            sum(self.rows_per_second_samples) / len(self.rows_per_second_samples)
        )
        if previous_rows_per_second is None:
            self.previous_rows_per_second = smoothed_rows_per_second
            self.baseline_size = self.current_size
            self.baseline_rows_per_second = smoothed_rows_per_second
            self._try_schedule_rows_per_second_probe("shrink")
            return

        baseline_rows_per_second = self.baseline_rows_per_second
        if baseline_rows_per_second is None or not self.is_experimental_size:
            self.baseline_size = self.current_size
            self.baseline_rows_per_second = smoothed_rows_per_second
            self._try_schedule_rows_per_second_probe("shrink")
            self.previous_rows_per_second = smoothed_rows_per_second
            return

        comparison = self._compare_rows_per_second(
            smoothed_rows_per_second,
            baseline_rows_per_second,
        )
        if self.probe_direction == "shrink":
            if comparison == "better":
                self._accept_rows_per_second_probe(smoothed_rows_per_second)
                self._try_schedule_rows_per_second_probe("shrink")
            else:
                self._restore_rows_per_second_baseline()
                self._try_schedule_rows_per_second_probe("grow")
        elif self.probe_direction == "grow":
            if comparison == "worse":
                self._restore_rows_per_second_baseline()
                self.probe_direction = None
                self.is_experimental_size = False
            else:
                self._accept_rows_per_second_probe(smoothed_rows_per_second)
                self._try_schedule_rows_per_second_probe("grow")
        self.previous_rows_per_second = smoothed_rows_per_second

    def _compare_rows_per_second(
        self,
        rows_per_second: float,
        baseline_rows_per_second: float,
    ) -> str:
        if rows_per_second < baseline_rows_per_second * (
            1.0 - self.target_rows_per_second_deadband
        ):
            return "worse"
        if rows_per_second > baseline_rows_per_second * (
            1.0 + self.target_rows_per_second_deadband
        ):
            return "better"
        return "equivalent"

    def _accept_rows_per_second_probe(self, rows_per_second: float) -> None:
        self.baseline_size = self.current_size
        self.baseline_rows_per_second = rows_per_second
        self.probe_direction = None
        self.is_experimental_size = False

    def _restore_rows_per_second_baseline(self) -> None:
        if self.baseline_size is not None:
            self.current_size = self.baseline_size
        self.probe_direction = None
        self.is_experimental_size = False

    def _try_schedule_rows_per_second_probe(self, direction: str) -> bool:
        baseline_size = self.baseline_size
        if baseline_size is None:
            return False
        if (
            self.noop_probe_size == baseline_size
            and self.noop_probe_direction == direction
        ):
            return False

        step_delta = max(1, int(baseline_size * self.adaptive_batch_size_step))
        if direction == "shrink":
            probe_size = max(self.min_size, baseline_size - step_delta)
        else:
            probe_size = self._cap_size(baseline_size + step_delta)

        if probe_size == baseline_size:
            self.probe_direction = None
            self.is_experimental_size = False
            self.current_size = baseline_size
            self.noop_probe_size = baseline_size
            self.noop_probe_direction = direction
            return False

        self.current_size = probe_size
        self.probe_direction = direction
        self.is_experimental_size = True
        return True

    def _update_for_memory(self, memory_bytes: int) -> None:
        target_memory_bytes = self._resolve_target_memory_bytes()
        if target_memory_bytes is None:
            return

        if memory_bytes < target_memory_bytes / 2:
            grown_size = max(self.current_size + 1, (self.current_size * 3 + 1) // 2)
            self.current_size = self._cap_size(grown_size)
            return

        if memory_bytes > target_memory_bytes:
            shrink_ratio = target_memory_bytes / memory_bytes
            shrunk_size = max(1, int(self.current_size * shrink_ratio))
            if shrunk_size >= self.current_size:
                shrunk_size = self.current_size - 1
            self.current_size = max(shrunk_size, self.min_size)

    def _resolve_target_memory_bytes(self) -> int | None:
        target_memory_bytes = self.target_memory_bytes
        if target_memory_bytes is None:
            return None
        min_target_memory_bytes = self.min_target_memory_bytes
        if min_target_memory_bytes is not None and target_memory_bytes < min_target_memory_bytes:
            target_memory_bytes = min_target_memory_bytes
        max_target_memory_bytes = self.max_target_memory_bytes
        if max_target_memory_bytes is not None and target_memory_bytes > max_target_memory_bytes:
            target_memory_bytes = max_target_memory_bytes
        return target_memory_bytes

    def _resolve_target_seconds(self) -> float:
        target_seconds = self.target_seconds
        min_target_seconds = self.min_target_seconds
        if min_target_seconds is not None and target_seconds < min_target_seconds:
            target_seconds = min_target_seconds
        max_target_seconds = self.max_target_seconds
        if max_target_seconds is not None and target_seconds > max_target_seconds:
            target_seconds = max_target_seconds
        return target_seconds

    def _cap_size(self, size: int) -> int:
        if self.max_size is None:
            return size
        return min(size, self.max_size)


def make_gp_insert_chunk_sizer(options: TransferOptions) -> AdaptiveBatchSizer:
    initial_size = options.gp_insert_chunk_size or DEFAULT_GP_INSERT_CHUNK_SIZE
    return AdaptiveBatchSizer(
        enabled=options.adaptive_batch_size,
        current_size=initial_size,
        min_size=min(1_000, initial_size),
        max_size=max(100_000, initial_size * 4),
        target_seconds=options.target_batch_seconds,
        optimize_by_rows_per_second=True,
        target_rows_per_second_window=options.target_rows_per_second_window,
        target_rows_per_second_deadband=options.target_rows_per_second_deadband,
        adaptive_batch_size_step=options.adaptive_batch_size_step,
    )


@dataclass(frozen=True)
class TransferOptions:
    from_db_key: str
    from_db_backend: str
    to_db_key: str
    to_db_backend: str
    source_sql: str
    target_table: str
    table_schema: dict[str, str] | None = None
    replace_target_table: bool = True
    write_mode: str = "replace"
    batch_size: int = 100_000
    retry_cnt: int = 5
    timeout_increment: int | float = 5
    full_retry_cnt: int = 5
    full_timeout_increment: int | float = 60 * 10
    key_columns: list[str] | None = None
    gp_distributed_by_key: list[str] | None = None
    gp_insert_chunk_size: int | None = None
    trino_insert_chunk_size: int | None = None
    adaptive_batch_size: bool = True
    min_batch_size: int = 1_000
    max_batch_size: int | None = 400_000
    target_batch_seconds: float = 10.0
    min_batch_seconds: float | None = None
    max_batch_seconds: float | None = None
    target_rows_per_second: bool = True
    target_rows_per_second_window: int = 5
    target_rows_per_second_deadband: float = 0.15
    adaptive_batch_size_step: float = 0.1
    target_batch_memory_mb: float | None = None
    target_batch_memory_bytes: int | None = None
    min_batch_memory_mb: float | None = None
    min_batch_memory_bytes: int | None = None
    max_batch_memory_mb: float | None = None
    max_batch_memory_bytes: int | None = None
    partition_by: list[str] | str | None = None
    order_by: list[str] | str | None = None
    ch_engine: str = "ReplicatedMergeTree"
    ch_cluster: str = "{cluster}"
    ch_sharding_key: str = "rand()"
    ch_only_shard: bool = False
    ch_retry_per_host_drops: bool = True
    query_label: str | None = None
    progress: bool = False
    estimate_total_rows: bool = False
    transfer_staging_schema: str | None = None
    transfer_staging_username: str | None = None


@dataclass
class TransferStageState:
    target_exists: bool
    stage_table_created: bool = False
    first_non_empty_batch: pd.DataFrame | None = None
    source_column_types: dict[str, str | None] | None = None
    stage_column_types: dict[str, str] | None = None
    insert_column_types: dict[str, str] | None = None
    stage_table: str | None = None


@dataclass
class TransferConnectionRefs:
    source: dict[str, Any] = field(default_factory=dict)
    target: dict[str, Any] = field(default_factory=dict)
