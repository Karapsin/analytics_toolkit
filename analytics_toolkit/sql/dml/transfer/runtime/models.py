from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


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
    target_memory_bytes: int | None = None

    def update(
        self,
        duration_seconds: float,
        *,
        memory_bytes: int | None = None,
    ) -> None:
        if not self.enabled:
            return

        if self.target_memory_bytes is not None:
            if memory_bytes is None:
                return
            self._update_for_memory(memory_bytes)
            return

        if duration_seconds < self.target_seconds / 2:
            grown_size = max(self.current_size + 1, (self.current_size * 3 + 1) // 2)
            self.current_size = self._cap_size(grown_size)
            return

        if duration_seconds > self.target_seconds * 2:
            shrunk_size = max(1, int(self.current_size * 0.5))
            self.current_size = max(shrunk_size, self.min_size)

    def _update_for_memory(self, memory_bytes: int) -> None:
        target_memory_bytes = self.target_memory_bytes
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

    def _cap_size(self, size: int) -> int:
        if self.max_size is None:
            return size
        return min(size, self.max_size)


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
    trino_insert_chunk_size: int | None = None
    adaptive_batch_size: bool = True
    min_batch_size: int = 1_000
    max_batch_size: int | None = 400_000
    target_batch_seconds: float = 10.0
    target_batch_memory_mb: float | None = None
    target_batch_memory_bytes: int | None = None
    partition_by: list[str] | str | None = None
    order_by: list[str] | str | None = None
    ch_engine: str = "ReplicatedMergeTree"
    ch_cluster: str = "{cluster}"
    ch_sharding_key: str = "rand()"
    only_shard: bool = False
    ch_retry_per_host_drops: bool = True
    ch_retry_per_host_drops_concurrency: int | None = 5
    query_label: str | None = None
    progress: bool = True
    estimate_total_rows: bool = False


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
