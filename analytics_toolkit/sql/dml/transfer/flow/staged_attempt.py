from __future__ import annotations

# ruff: noqa: BLE001, EM101, EM102, I001, PLR0913, PLR0915, PLR2004, S608, TC006, TID252, TRY003, TRY300, TRY301

import contextvars
import math
import time
import uuid
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import replace
from typing import Any, cast

import pandas as pd
from tqdm import tqdm

from analytics_toolkit.general import time_print

from ...._log_context import sql_log_context
from ....backends import get_backend_adapter
from ....backends.transfer_stage import (
    collision_stage_suffix,
    execute_transfer_materialization,
)
from ....connection.get_sql_connection import get_sql_connection
from ....dml.io.read_sql import _read_backend
from ....ddl.schema import validate_table_schema_columns
from ...load.load_sql_table import insert_rows_batch
from ...load.stage import build_stage_table_name, cleanup_stage_table, create_stage_table
from ...table._basic_ops import insert_from_table, table_exists
from ..runtime.models import (
    AdaptiveBatchSizer,
    RowBatch,
    TransferConnectionRefs,
    TransferOptions,
    TransferSliceRowCount,
    TransferStageState,
)
from ..runtime.retry import (
    close_connection_ref,
    close_connection_refs_preserving,
    replace_connection,
    rollback_quietly,
    run_with_retry,
)
from ..schema import inspect_source_query_schema, map_source_schema_to_target
from .finalize import cleanup_stage, finalize_loaded_stage
from .progress import make_transfer_progress_bar
from .range_scheduler import AdaptiveRangeScheduler, OrdinalRange
from .source_snapshot import (
    build_append_snapshot_slice_sql,
    build_snapshot_range_sql,
    build_snapshot_select_sql,
    build_source_snapshot_sql,
)
from .stage import _with_internal_column_types, create_stage_state, ensure_transfer_target_table
from .stage_identity import resolve_internal_columns
from .staged_keyed_pipeline import run_keyed_staged_source_transfer_attempt
from .staged_unkeyed_progress import UnkeyedStagedProgress
from .superseded import cleanup_superseded_transfer_stages


def run_staged_source_transfer_attempt(
    options: TransferOptions,
    *,
    insert_retry_cnt: int,
) -> int:
    if options.transfer_id is None or options.canonical_destination_identity is None:
        raise RuntimeError("Transfer runtime identity must be initialized.")
    if options.transfer_slices is not None:
        return run_keyed_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=insert_retry_cnt,
        )
    attempt_started_at = time.monotonic()
    source_ref: dict[str, Any] = {}
    target_ref: dict[str, Any] = {}
    refs = TransferConnectionRefs(source=source_ref, target=target_ref)
    stage_state: TransferStageState | None = None
    snapshot_table: str | None = None
    stage_tables: list[str] = []
    transfer_progress: UnkeyedStagedProgress | None = None
    transfer_completed = False
    cleanup_succeeded = False
    error: BaseException | None = None
    attempt_phase = "metadata inspection"
    try:
        source_ref["connection"] = get_sql_connection(options.from_db_key)
        target_ref["connection"] = get_sql_connection(options.to_db_key)
        stage_state = create_stage_state(options, refs)
        representative_sql = (
            options.transfer_slices[0].source_sql if options.transfer_slices else options.source_sql
        )
        source_schema = inspect_source_query_schema(
            options.from_db_backend,
            source_ref["connection"],
            representative_sql,
        )
        source_columns = [column.name for column in source_schema]
        if not source_columns:
            raise ValueError(
                "Source staging requires an inspectable source schema with at least one column."
            )
        stage_state.source_columns = source_columns
        stage_state.source_column_types = {
            column.name: column.native_type for column in source_schema
        }
        stage_state.internal_columns = resolve_internal_columns(
            source_columns,
            options.from_db_backend,
            table_schema_names=(options.table_schema or {}).keys(),
        )
        cleanup_superseded_transfer_stages(
            options=options,
            connection=source_ref["connection"],
            backend=options.from_db_backend,
            connection_key=options.from_db_key,
            staging_schema=options.source_transfer_staging_schema,
            internal_columns=stage_state.internal_columns,
        )
        cleanup_superseded_transfer_stages(
            options=options,
            connection=target_ref["connection"],
            backend=options.to_db_backend,
            connection_key=options.to_db_key,
            staging_schema=options.transfer_staging_schema,
            internal_columns=stage_state.internal_columns,
        )
        source_types = (
            validate_table_schema_columns(options.table_schema, source_columns)
            if options.table_schema is not None
            else map_source_schema_to_target(
                source_schema,
                options.to_db_backend,
                source_backend=options.from_db_backend,
            )
        )
        stage_state.stage_column_types = _with_internal_column_types(
            source_types,
            options,
            stage_state,
        )
        ensure_transfer_target_table(options, refs, stage_state, source_columns)
        attempt_phase = "source-stage loading"
        snapshot_table, slice_counts = _materialize_snapshot(
            options,
            source_ref,
            stage_state,
        )
        stage_state.source_stage_tables = [snapshot_table]
        total_rows = sum(slice_counts.values())
        worker_count = _effective_transfer_worker_count(
            min(
                options.transfer_concurrency.effective_read,
                options.transfer_concurrency.effective_write,
            ),
            total_rows,
            options.batch_size,
        )
        object.__setattr__(
            options,
            "transfer_concurrency",
            replace(
                options.transfer_concurrency,
                effective_read=worker_count,
                effective_write=worker_count,
            ),
        )
        time_print(
            "Transfer worker selection: "
            f"requested={options.transfer_concurrency.requested_read}, "
            f"soft_limited={options.transfer_concurrency.soft_limited_read}, "
            f"effective={worker_count}",
            phase="select_workers",
        )
        transfer_progress = UnkeyedStagedProgress(
            options,
            total_rows=total_rows,
            worker_count=worker_count,
            attempt_started_at=attempt_started_at,
            progress_bar=make_transfer_progress_bar(
                options,
                total=total_rows,
                base_tqdm=tqdm,
            ),
        )
        replace_connection(options.to_db_key, target_ref)
        stage_tables = _create_worker_stages(
            options,
            target_ref,
            stage_state,
            worker_count=worker_count,
        )
        close_connection_ref(source_ref, options.from_db_key, "source coordinator")
        source_ref.pop("connection", None)
        close_connection_ref(target_ref, options.to_db_key, "target coordinator")
        target_ref.pop("connection", None)
        scheduler = AdaptiveRangeScheduler(slice_counts)
        _run_range_workers(
            options,
            snapshot_table,
            source_columns,
            stage_state,
            stage_tables,
            scheduler,
            insert_retry_cnt=insert_retry_cnt,
            transfer_progress=transfer_progress,
        )
        attempt_phase = "aggregate stage validation"
        scheduler.validate_complete()
        transfer_progress.mark_loading_complete()
        stage_state.slice_counts = [
            TransferSliceRowCount(
                index=slice_id,
                label=None,
                expected_rows=count,
                streamed_rows=count,
            )
            for slice_id, count in sorted(slice_counts.items())
        ]
        attempt_phase = "target-stage consolidation"
        consolidation_started_at = transfer_progress.now()
        _consolidate_worker_stages(options, target_ref, stage_state, stage_tables)
        transfer_progress.mark_consolidation_complete(
            stage_count=len(stage_tables),
            copied_rows=transfer_progress.expected_consolidation_rows,
            elapsed_seconds=transfer_progress.now() - consolidation_started_at,
        )
        close_connection_ref(target_ref, options.to_db_key, "target consolidation")
        target_ref.pop("connection", None)
        attempt_phase = "destination finalization"
        transfer_progress.mark_finalization_started()
        finalize_loaded_stage(options, refs, stage_state, total_rows)
        transfer_progress.mark_finalization_complete()
        transfer_completed = True
        return total_rows
    except BaseException as exc:
        error = exc
        _attach_unkeyed_attempt_failure(
            exc,
            transfer_progress,
            phase=attempt_phase,
            attempt_started_at=attempt_started_at,
        )
        raise
    finally:
        cleanup_error: BaseException | None = None
        try:
            cleanup_succeeded = _cleanup_unkeyed_attempt(
                options,
                refs,
                stage_state,
                source_ref,
                target_ref,
                snapshot_table=snapshot_table,
                error=error,
            )
        except BaseException as exc:
            cleanup_error = exc
        progress_error: BaseException | None = None
        try:
            _finish_unkeyed_progress(
                transfer_progress,
                transfer_completed=transfer_completed,
                cleanup_succeeded=cleanup_succeeded,
                snapshot_table=snapshot_table,
                stage_tables=stage_tables,
            )
        except BaseException as exc:
            progress_error = exc
        if error is None:
            if cleanup_error is not None:
                _attach_unkeyed_attempt_failure(
                    cleanup_error,
                    transfer_progress,
                    phase=attempt_phase,
                    attempt_started_at=attempt_started_at,
                )
                raise cleanup_error.with_traceback(cleanup_error.__traceback__)
            if progress_error is not None:
                _attach_unkeyed_attempt_failure(
                    progress_error,
                    transfer_progress,
                    phase=attempt_phase,
                    attempt_started_at=attempt_started_at,
                )
                raise progress_error.with_traceback(progress_error.__traceback__)


def _cleanup_unkeyed_attempt(  # noqa: C901
    options: TransferOptions,
    refs: TransferConnectionRefs,
    stage_state: TransferStageState | None,
    source_ref: dict[str, Any],
    target_ref: dict[str, Any],
    *,
    snapshot_table: str | None,
    error: BaseException | None,
) -> bool:
    cleanup_error: BaseException | None = None
    try:
        close_connection_ref(target_ref, options.to_db_key, "target coordinator")
    except BaseException as exc:
        cleanup_error = exc
    else:
        target_ref.pop("connection", None)
    if stage_state is not None:
        try:
            cleanup_stage(
                options,
                refs,
                stage_state,
                options.retry_cnt,
                drop_created_target=error is not None,
            )
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
    if snapshot_table is not None:
        try:
            if source_ref.get("connection") is None:
                source_ref["connection"] = get_sql_connection(options.from_db_key)
            cleanup_stage_table(
                options.from_db_backend,
                source_ref["connection"],
                snapshot_table,
                query_label=options.query_label,
            )
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
    try:
        close_connection_ref(source_ref, options.from_db_key, "source")
    except BaseException as exc:
        cleanup_error = cleanup_error or exc
    try:
        close_connection_ref(target_ref, options.to_db_key, "target")
    except BaseException as exc:
        cleanup_error = cleanup_error or exc
    if cleanup_error is not None:
        raise cleanup_error.with_traceback(cleanup_error.__traceback__)
    return True


def _finish_unkeyed_progress(
    transfer_progress: UnkeyedStagedProgress | None,
    *,
    transfer_completed: bool,
    cleanup_succeeded: bool,
    snapshot_table: str | None,
    stage_tables: list[str],
) -> None:
    if transfer_progress is None:
        return
    try:
        if transfer_completed and cleanup_succeeded:
            transfer_progress.log_transfer_complete(
                source_stages_dropped=int(snapshot_table is not None),
                target_stages_cleaned=len(stage_tables),
            )
    finally:
        transfer_progress.close()


def _attach_unkeyed_attempt_failure(
    error: BaseException,
    transfer_progress: UnkeyedStagedProgress | None,
    *,
    phase: str,
    attempt_started_at: float,
) -> None:
    if transfer_progress is not None:
        transfer_progress.attach_attempt_summary(error, phase)
        return
    try:
        error.__dict__["analytics_toolkit_transfer_attempt_summary"] = {
            "phase": phase,
            "committed_rows": 0,
            "elapsed_seconds": max(time.monotonic() - attempt_started_at, 0.0),
        }
    except Exception:
        return


def _materialize_snapshot(
    options: TransferOptions,
    source_ref: dict[str, Any],
    stage_state: TransferStageState,
) -> tuple[str, dict[int, int]]:
    staging_schema = options.source_transfer_staging_schema
    internal = stage_state.internal_columns
    if staging_schema is None or internal is None:
        raise RuntimeError("Source snapshot configuration is incomplete.")
    snapshot_table = _allocate_snapshot_name(options, source_ref)
    slices = options.transfer_slices or []
    source_sqls = (
        [(item.index, item.source_sql) for item in slices] if slices else [(0, options.source_sql)]
    )
    adapter = get_backend_adapter(options.from_db_backend)
    created = False
    try:
        for slice_id, source_sql in source_sqls:
            select_sql = build_snapshot_select_sql(
                backend=options.from_db_backend,
                source_sql=source_sql,
                source_columns=stage_state.source_columns,
                transfer_id=options.transfer_id or "",
                canonical_destination=options.canonical_destination_identity or "",
                slice_id=slice_id,
                internal_columns=internal,
            )
            if not created:
                snapshot_sql = build_source_snapshot_sql(
                    backend=options.from_db_backend,
                    snapshot_table=snapshot_table,
                    snapshot_select_sql=select_sql,
                    internal_columns=internal,
                )
                execute_transfer_materialization(
                    adapter,
                    options.from_db_backend,
                    source_ref["connection"],
                    snapshot_sql.create_sql,
                )
                created = True
            else:
                execute_transfer_materialization(
                    adapter,
                    options.from_db_backend,
                    source_ref["connection"],
                    build_append_snapshot_slice_sql(
                        backend=options.from_db_backend,
                        snapshot_table=snapshot_table,
                        source_columns=stage_state.source_columns,
                        internal_columns=internal,
                        snapshot_select_sql=select_sql,
                    ),
                )
        snapshot_sql = build_source_snapshot_sql(
            backend=options.from_db_backend,
            snapshot_table=snapshot_table,
            snapshot_select_sql="SELECT 1",
            internal_columns=internal,
        )
        for sql in snapshot_sql.post_create_sqls:
            adapter.execute_command(source_ref["connection"], sql)
        return snapshot_table, _snapshot_slice_counts(
            options,
            source_ref["connection"],
            snapshot_table,
            internal.slice_id,
        )
    except BaseException:
        if created:
            try:
                adapter.drop_table(
                    source_ref["connection"],
                    snapshot_table,
                    if_exists=True,
                    query_label=options.query_label,
                )
            except BaseException:
                time_print(
                    "[slice=1/1] Failed to drop an incomplete source snapshot; "
                    "startup cleanup will retry it",
                    level="warning",
                )
        raise


def _allocate_snapshot_name(
    options: TransferOptions,
    source_ref: dict[str, Any],
) -> str:
    preferred = f"{options.transfer_id}__source"
    for attempt in range(10):
        suffix = (
            preferred
            if attempt == 0
            else collision_stage_suffix(
                options.from_db_backend,
                preferred,
                uuid.uuid4().hex,
            )
        )
        candidate = build_stage_table_name(
            options.from_db_backend,
            options.target_table,
            transfer_staging_schema=options.source_transfer_staging_schema,
            transfer_staging_username=options.source_transfer_staging_username,
            random_suffix=suffix,
            destination_hash=options.destination_hash,
        )
        if not table_exists(
            options.from_db_backend,
            source_ref["connection"],
            candidate,
            connection_key=options.from_db_key,
        ):
            return candidate
    raise RuntimeError("Could not allocate a unique source snapshot table name.")


def _snapshot_slice_counts(
    options: TransferOptions,
    connection: Any,
    snapshot_table: str,
    slice_column_name: str,
) -> dict[int, int]:
    adapter = get_backend_adapter(options.from_db_backend)
    slice_column = adapter.quote_identifier(slice_column_name)
    result = _read_backend(
        options.from_db_backend,
        connection,
        f"SELECT {slice_column}, COUNT(*) FROM {snapshot_table} GROUP BY {slice_column}",
        print_queries=False,
        output_type="dict",
        action_name="snapshot counting",
        phase="count_snapshot",
    )
    counts = {
        int(slice_id): int(count) for slice_id, count in zip(result.columns[0], result.columns[1])
    }
    expected_slices = (
        [item.index for item in options.transfer_slices] if options.transfer_slices else [0]
    )
    return {slice_id: counts.get(slice_id, 0) for slice_id in expected_slices}


def _effective_transfer_worker_count(
    requested_concurrency: int,
    total_rows: int,
    batch_size: int,
) -> int:
    batch_count = max(1, math.ceil(total_rows / batch_size))
    return min(requested_concurrency, batch_count)


def _create_worker_stages(
    options: TransferOptions,
    target_ref: dict[str, Any],
    stage_state: TransferStageState,
    *,
    worker_count: int,
) -> list[str]:
    sample = pd.DataFrame(
        columns=[
            *stage_state.source_columns,
            *(stage_state.internal_columns.names() if stage_state.internal_columns else ()),
        ]
    )
    stage_state.first_non_empty_batch = pd.DataFrame(columns=stage_state.source_columns)
    worker_tables: list[str] = []
    registered_candidates: list[str] = []

    def register_candidate(stage_table: str) -> None:
        if stage_table in registered_candidates:
            return
        registered_candidates.append(stage_table)
        stage_state.stage_table = registered_candidates[0]
        stage_state.stage_tables = list(registered_candidates)
        stage_state.stage_table_created = True

    for worker_id in range(worker_count):
        stage_table = create_stage_table(
            options.to_db_backend,
            target_ref["connection"],
            options.target_table,
            sample,
            column_types=stage_state.stage_column_types,
            connection_key=options.to_db_key,
            query_label=options.query_label,
            transfer_staging_schema=options.transfer_staging_schema,
            transfer_staging_username=options.transfer_staging_username,
            random_suffix=f"{options.transfer_id}__w{worker_id:05d}",
            destination_hash=options.destination_hash,
            on_stage_candidate=register_candidate,
            ddl_properties=options.staging_ddl_properties,
            ch_creation_policy=options.staging_ch_policy,
        )
        register_candidate(stage_table)
        worker_tables.append(stage_table)

    for candidate in registered_candidates:
        if candidate in worker_tables:
            continue
        cleanup_stage_table(
            options.to_db_backend,
            target_ref["connection"],
            candidate,
            query_label=options.query_label,
            ch_creation_policy=options.staging_ch_policy,
        )
    stage_state.stage_table = worker_tables[0]
    stage_state.stage_tables = list(worker_tables)
    stage_state.stage_table_created = True
    return worker_tables


def _run_range_workers(
    options: TransferOptions,
    snapshot_table: str,
    source_columns: list[str],
    stage_state: TransferStageState,
    stage_tables: list[str],
    scheduler: AdaptiveRangeScheduler,
    *,
    insert_retry_cnt: int,
    transfer_progress: UnkeyedStagedProgress | None = None,
) -> None:
    with ThreadPoolExecutor(max_workers=len(stage_tables)) as executor:
        worker_function = (
            _range_worker if transfer_progress is None else _range_worker_with_progress
        )
        pending = {
            executor.submit(
                contextvars.copy_context().run,
                worker_function,
                options,
                snapshot_table,
                source_columns,
                stage_state,
                stage_table,
                scheduler,
                worker_id,
                insert_retry_cnt,
                *(() if transfer_progress is None else (transfer_progress,)),
            )
            for worker_id, stage_table in enumerate(stage_tables)
        }
        while pending:
            done, pending = wait(pending, return_when=FIRST_EXCEPTION)
            for future in done:
                if future.exception() is not None:
                    for other in pending:
                        other.cancel()
                    raise future.exception()  # type: ignore[misc]


def _range_worker(
    options: TransferOptions,
    snapshot_table: str,
    source_columns: list[str],
    stage_state: TransferStageState,
    stage_table: str,
    scheduler: AdaptiveRangeScheduler,
    worker_id: int,
    insert_retry_cnt: int,
    *,
    transfer_progress: UnkeyedStagedProgress | None = None,
) -> None:
    source_ref: dict[str, Any] = {}
    target_ref: dict[str, Any] = {}
    sizer = AdaptiveBatchSizer(
        enabled=options.adaptive_batch_size,
        current_size=options.batch_size,
        min_size=options.min_batch_size,
        max_size=options.max_batch_size,
        target_seconds=options.target_batch_seconds,
    )
    source_failures: dict[tuple[int, int, int], int] = {}
    worker_error: BaseException | None = None
    try:
        source_ref["connection"] = get_sql_connection(options.from_db_key)
        target_ref["connection"] = get_sql_connection(options.to_db_key)
        while True:
            claimed = scheduler.claim(worker_id, sizer.current_size)
            if claimed is None:
                return
            read_started_at = transfer_progress.now() if transfer_progress is not None else 0.0
            try:
                batch = _read_snapshot_range(
                    options,
                    source_ref["connection"],
                    snapshot_table,
                    source_columns,
                    stage_state,
                    claimed,
                )
            except Exception:
                key = (claimed.slice_id, claimed.start_ordinal, claimed.stop_ordinal)
                source_failures[key] = source_failures.get(key, 0) + 1
                if source_failures[key] >= options.retry_cnt:
                    raise
                reduced = max(options.min_batch_size, claimed.row_count // 2)
                scheduler.requeue_failed(
                    worker_id,
                    claimed,
                    reduced_batch_size=reduced,
                )
                rollback_quietly(source_ref["connection"])
                replace_connection(options.from_db_key, source_ref)
                committed_rows = (
                    transfer_progress.committed_rows if transfer_progress is not None else 0
                )
                time_print(
                    "[slice=1/1] Retrying source-stage range "
                    f"{claimed.slice_id}:{claimed.start_ordinal}-{claimed.stop_ordinal} read: "
                    f"attempt {source_failures[key] + 1}/{options.retry_cnt}; committed total "
                    f"remains {committed_rows:,} rows; ETA unchanged"
                )
                continue
            read_completed_at = transfer_progress.now() if transfer_progress is not None else 0.0
            if batch.row_count != claimed.row_count:
                raise RuntimeError(
                    f"Source snapshot range {claimed} returned {batch.row_count} row(s)."
                )
            logical_batch_id = (
                claimed.slice_id,
                claimed.start_ordinal,
                claimed.stop_ordinal,
            )
            _insert_unkeyed_range_batch(
                options,
                target_ref,
                stage_state,
                stage_table,
                batch,
                logical_batch_id,
                insert_retry_cnt=insert_retry_cnt,
                transfer_progress=transfer_progress,
            )
            if transfer_progress is not None:
                transfer_progress.commit_batch(
                    logical_batch_id=logical_batch_id,
                    worker_id=worker_id,
                    batch=batch,
                    read_started_at=read_started_at,
                    read_completed_at=read_completed_at,
                    insert_completed_at=transfer_progress.now(),
                )
            scheduler.complete(worker_id, claimed)
    except BaseException as exc:
        worker_error = exc
        raise
    finally:
        close_connection_refs_preserving(
            worker_error,
            (source_ref, options.from_db_key, f"source worker {worker_id}"),
            (target_ref, options.to_db_key, f"target worker {worker_id}"),
        )


def _insert_unkeyed_range_batch(
    options: TransferOptions,
    target_ref: dict[str, Any],
    stage_state: TransferStageState,
    stage_table: str,
    batch: RowBatch,
    logical_batch_id: tuple[int, int, int],
    *,
    insert_retry_cnt: int,
    transfer_progress: UnkeyedStagedProgress | None,
) -> None:
    def retry_insert(**kwargs: Any) -> Any:
        if transfer_progress is None:
            return run_with_retry(**kwargs)
        return run_with_retry(
            **kwargs,
            log_prefix=transfer_progress.log_prefix,
            safe_exception_logging=True,
            retry_status=lambda attempt, total: transfer_progress.target_insert_retry_status(
                logical_batch_id,
                attempt,
                total,
            ),
        )

    insert_rows_batch(
        options.to_db_backend,
        target_ref,
        stage_table,
        batch.columns,
        batch.rows,
        retry_fn=retry_insert,
        retry_cnt=insert_retry_cnt,
        timeout_increment=options.timeout_increment,
        target_column_types=stage_state.stage_column_types,
        query_label=options.query_label,
        connection_key=options.to_db_key,
        rollback_fn=rollback_quietly,
        replace_connection_fn=replace_connection,
        safe_exception_logging=transfer_progress is not None,
        log_prefix=transfer_progress.log_prefix if transfer_progress is not None else "",
    )


def _range_worker_with_progress(
    options: TransferOptions,
    snapshot_table: str,
    source_columns: list[str],
    stage_state: TransferStageState,
    stage_table: str,
    scheduler: AdaptiveRangeScheduler,
    worker_id: int,
    insert_retry_cnt: int,
    transfer_progress: UnkeyedStagedProgress,
) -> None:
    _range_worker(
        options,
        snapshot_table,
        source_columns,
        stage_state,
        stage_table,
        scheduler,
        worker_id,
        insert_retry_cnt,
        transfer_progress=transfer_progress,
    )


def _read_snapshot_range(
    options: TransferOptions,
    connection: Any,
    snapshot_table: str,
    source_columns: list[str],
    stage_state: TransferStageState,
    claimed: OrdinalRange,
) -> RowBatch:
    internal = stage_state.internal_columns
    if internal is None:
        raise RuntimeError("Transfer internal columns were not resolved.")
    sql = build_snapshot_range_sql(
        backend=options.from_db_backend,
        snapshot_table=snapshot_table,
        source_columns=source_columns,
        internal_columns=internal,
        transfer_id=options.transfer_id or "",
        canonical_destination=options.canonical_destination_identity or "",
        ordinal_range=claimed,
    )
    with sql_log_context("[slice=1/1] ", suppress_sql=True):
        result = _read_backend(
            options.from_db_backend,
            connection,
            sql,
            print_queries=False,
            output_type="dict",
            action_name="source-batch reading",
            phase="read_source_batch",
        )
    result_row_count = len(result.columns[0]) if result.columns else 0
    if any(len(column) != result_row_count for column in result.columns):
        raise RuntimeError("[slice=1/1] Source batch columns have unequal lengths.")
    if result_row_count > claimed.row_count:
        raise RuntimeError(
            f"[slice=1/1] Source batch returned {result_row_count} row(s); "
            f"scheduled limit is {claimed.row_count}."
        )
    batch = RowBatch(
        columns=list(result.column_names),
        rows=list(zip(*result.columns)),
    )
    normalized = cast(
        RowBatch,
        get_backend_adapter(
            options.from_db_backend,
        ).normalize_transfer_source_batch(
            batch,
            stage_state.source_column_types or {},
        ),
    )
    if normalized.row_count > claimed.row_count:
        raise RuntimeError(
            f"[slice=1/1] Normalized source batch returned {normalized.row_count} row(s); "
            f"scheduled limit is {claimed.row_count}."
        )
    return normalized


def _consolidate_worker_stages(
    options: TransferOptions,
    target_ref: dict[str, Any],
    stage_state: TransferStageState,
    stage_tables: list[str],
) -> None:
    if options.write_mode == "upsert" or len(stage_tables) < 2:
        return
    replace_connection_fn = target_ref.get(
        "bounded_replace_connection",
        replace_connection,
    )
    replace_connection_fn(options.to_db_key, target_ref)
    for stage_table in stage_tables[1:]:
        insert_from_table(
            options.to_db_backend,
            target_ref["connection"],
            stage_tables[0],
            stage_table,
            column_types=stage_state.stage_column_types,
            query_label=options.query_label,
        )
