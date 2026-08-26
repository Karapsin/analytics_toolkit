from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import pandas as pd
import pytest
from analytics_toolkit.general import time_print
from analytics_toolkit.sql._log_context import sql_log_context
from analytics_toolkit.sql.backends import common_methods
from analytics_toolkit.sql.backends.ch import lifecycle as ch_lifecycle
from analytics_toolkit.sql.backends.ch.ddl import build_ch_shard_table_name
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.connection.errors import SqlTableReadinessError
from analytics_toolkit.sql.dml.load import stage as load_stage
from analytics_toolkit.sql.dml.transfer.flow import (
    api as transfer_api,
)
from analytics_toolkit.sql.dml.transfer.flow import (
    dry_run,
    finalize,
    row_counts,
    staged_attempt,
    staged_keyed_io,
    staged_keyed_logging,
    staged_keyed_pipeline,
)
from analytics_toolkit.sql.dml.transfer.flow.lazy_keyed_runtime import (
    DropReady,
    KeyReadComplete,
    LazyKeyedRuntime,
    QueuedKeyBatch,
    ReadyKeyTask,
    VerifiedKey,
    freeze_attempt_metadata,
)
from analytics_toolkit.sql.dml.transfer.flow.stage_identity import resolve_internal_columns
from analytics_toolkit.sql.dml.transfer.flow.transfer_progress import TransferProgressTracker
from analytics_toolkit.sql.dml.transfer.runtime import retry as transfer_retry
from analytics_toolkit.sql.dml.transfer.runtime.connection_pool import (
    BoundedConnectionCloseError,
    BoundedConnectionManager,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    RowBatch,
    TransferConcurrency,
    TransferConnectionRefs,
    TransferOptions,
    TransferSlice,
    TransferStageState,
)
from analytics_toolkit.sql.execution.operation_runner import tracked_sql_operation
from analytics_toolkit.sql.execution.plans import SqlPlan


def _concurrency(read: int = 2, write: int = 2) -> TransferConcurrency:
    return TransferConcurrency(
        legacy_value=None,
        requested_read=read,
        requested_write=write,
        effective_read=read,
        effective_write=write,
        split_requested=True,
    )


def _options(**overrides: Any) -> TransferOptions:
    slices = [
        TransferSlice(0, (1,), "", "SELECT 1 AS id", "key=1"),
        TransferSlice(1, (2,), "", "SELECT 2 AS id", "key=2"),
    ]
    values: dict[str, Any] = {
        "from_db_key": "source",
        "from_db_backend": "gp",
        "to_db_key": "target",
        "to_db_backend": "gp",
        "source_sql": "SELECT id FROM source",
        "target_table": "public.target",
        "transfer_id": "a" * 32,
        "canonical_destination_identity": "public.target",
        "destination_hash": "0123456789abcdef",
        "source_transfer_staging_schema": "source_stage",
        "transfer_staging_schema": "target_stage",
        "transfer_slices": slices,
        "transfer_keys": ["key"],
        "batch_size": 2,
        "min_batch_size": 1,
        "max_batch_size": 4,
        "adaptive_batch_size": False,
        "retry_cnt": 1,
        "transfer_concurrency": _concurrency(),
    }
    values.update(overrides)
    return TransferOptions(**values)


def _state() -> TransferStageState:
    return TransferStageState(
        target_exists=True,
        source_columns=["id"],
        source_column_types={"id": "bigint"},
        stage_column_types={"id": "BIGINT"},
        internal_columns=resolve_internal_columns(["id"], "gp"),
    )


def _metadata() -> Any:
    state = _state()
    assert state.internal_columns is not None
    return freeze_attempt_metadata(
        source_columns=state.source_columns or [],
        source_column_types=state.source_column_types or {},
        stage_column_types=state.stage_column_types,
        internal_columns=state.internal_columns,
    )


def _ready_task(
    transfer_slice: TransferSlice,
    source_stage: str,
    expected_rows: int,
) -> ReadyKeyTask:
    slice_values = transfer_slice.values  # noqa: PD011
    return ReadyKeyTask(
        transfer_slice=transfer_slice,
        source_stage=source_stage,
        expected_rows=expected_rows,
        tag=(f"[slice={transfer_slice.index + 1}/2 key=key:{slice_values[0]!r}]"),
        materialized_at=0.0,
    )


class _LeaseManager:
    def __init__(self) -> None:
        self.active = 0
        self.high_water_mark = 0
        self.lease_count = 0
        self.released = threading.Event()

    def interrupt_active(self) -> None:
        return

    def resume_for_cleanup(self) -> None:
        return

    def run(self, _role: str, operation: Callable[[dict[str, Any]], Any]) -> Any:
        with self.lease() as connection_ref:
            return operation(connection_ref)

    def close(self) -> None:
        return

    def close_preserving(self, error: BaseException | None) -> None:
        try:
            self.close()
        except BaseException:
            if error is None:
                raise
            error.__dict__["analytics_toolkit_sql_retry_safe"] = False

    @contextmanager
    def lease(
        self,
        *,
        cancellation: threading.Event | None = None,
    ) -> Iterator[dict[str, Any]]:
        if cancellation is not None and cancellation.is_set():
            raise RuntimeError("lease cancelled")
        self.active += 1
        self.lease_count += 1
        self.high_water_mark = max(self.high_water_mark, self.active)
        try:
            yield {"connection": object()}
        finally:
            self.active -= 1
            self.released.set()


class _ProgressBar:
    def update(self, _rows: int) -> None:
        return

    def close(self) -> None:
        return


def _thread(operation: Callable[[], None]) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            operation()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    return worker, errors


__all__ = [
    "Any",
    "BoundedConnectionCloseError",
    "BoundedConnectionManager",
    "Callable",
    "DropReady",
    "Iterator",
    "KeyReadComplete",
    "LazyKeyedRuntime",
    "QueuedKeyBatch",
    "ReadyKeyTask",
    "RowBatch",
    "SimpleNamespace",
    "SourceColumn",
    "SqlPlan",
    "SqlTableReadinessError",
    "TransferConcurrency",
    "TransferConnectionRefs",
    "TransferOptions",
    "TransferProgressTracker",
    "TransferSlice",
    "TransferStageState",
    "VerifiedKey",
    "_LeaseManager",
    "_ProgressBar",
    "_concurrency",
    "_metadata",
    "_options",
    "_ready_task",
    "_state",
    "_thread",
    "build_ch_shard_table_name",
    "ch_lifecycle",
    "common_methods",
    "contextmanager",
    "dry_run",
    "finalize",
    "freeze_attempt_metadata",
    "load_stage",
    "pd",
    "pytest",
    "resolve_internal_columns",
    "row_counts",
    "sql_log_context",
    "staged_attempt",
    "staged_keyed_io",
    "staged_keyed_logging",
    "staged_keyed_pipeline",
    "threading",
    "time",
    "time_print",
    "tracked_sql_operation",
    "transfer_api",
    "transfer_retry",
]
