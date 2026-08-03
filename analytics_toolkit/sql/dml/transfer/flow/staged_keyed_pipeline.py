from __future__ import annotations

# ruff: noqa: EM101, PLC0415, PLR0913, S608, TID252, TRY003
import contextvars
import threading
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, cast

import pandas as pd

from analytics_toolkit.general import time_print

from ....backends import get_backend_adapter
from ....backends.transfer_stage import collision_stage_suffix, execute_transfer_materialization
from ....connection.get_sql_connection import get_sql_connection
from ....ddl.schema import validate_table_schema_columns
from ....dml.io.read_sql import _read_backend
from ...load.load_sql_table import insert_rows_batch
from ...load.stage import build_stage_table_name, cleanup_stage_table, create_stage_table
from ...table._basic_ops import table_exists
from ..runtime.models import (
    AdaptiveBatchSizer,
    RowBatch,
    TransferConnectionRefs,
    TransferOptions,
    TransferSlice,
    TransferSliceRowCount,
    TransferStageState,
)
from ..runtime.retry import (
    close_connection_ref,
    replace_connection,
    rollback_quietly,
    run_with_retry,
)
from ..schema import inspect_source_query_schema, map_source_schema_to_target
from .finalize import cleanup_stage, finalize_loaded_stage
from .logging import (
    format_transfer_slice_log_label,
    pipeline_phase_message,
    staged_pipeline_start_message,
    staged_reader_slice_message,
    staged_writer_key_message,
)
from .range_scheduler import OrdinalRange
from .row_counts import validate_loaded_stage_row_count
from .source_snapshot import (
    build_append_snapshot_slice_sql,
    build_snapshot_select_sql,
    build_source_snapshot_sql,
)
from .stage import _with_internal_column_types, create_stage_state, ensure_transfer_target_table
from .stage_identity import resolve_internal_columns
from .stage_validation import validate_transfer_stage_identity
from .superseded import cleanup_superseded_transfer_stages


@dataclass(frozen=True)
class SourceStageResult:
    worker_index: int
    table: str
    slice_counts: dict[int, int]


@dataclass(frozen=True)
class WholeKeyTask:
    transfer_slice: TransferSlice
    source_stage: str
    row_count: int


def run_keyed_staged_source_transfer_attempt(  # noqa: PLR0915
    options: TransferOptions,
    *,
    insert_retry_cnt: int,
) -> int:
    if not options.transfer_slices:
        raise ValueError("Keyed source staging requires transfer slices.")
    if options.transfer_id is None or options.canonical_destination_identity is None:
        raise RuntimeError("Transfer runtime identity must be initialized.")

    source_ref = {"connection": get_sql_connection(options.from_db_key)}
    target_ref = {"connection": get_sql_connection(options.to_db_key)}
    refs = TransferConnectionRefs(source=source_ref, target=target_ref)
    stage_state = create_stage_state(options, refs)
    source_stage_tables: list[str] = []
    error: Exception | None = None
    cleanup_error: Exception | None = None
    try:
        _prepare_attempt(options, refs, stage_state)
        read_workers = options.transfer_concurrency.effective_read
        write_workers = options.transfer_concurrency.effective_write
        time_print(
            staged_pipeline_start_message(len(options.transfer_slices), read_workers, write_workers)
        )
        assignments = [
            options.transfer_slices[index::read_workers] for index in range(read_workers)
        ]
        preferred_tables = [
            _allocate_source_stage_name(options, source_ref, worker_index)
            for worker_index in range(read_workers)
        ]
        source_stage_tables.extend(preferred_tables)
        results = _run_source_stage_workers(
            options,
            stage_state,
            assignments,
            preferred_tables,
        )
        stage_state.source_stage_tables = list(source_stage_tables)
        slice_counts = {
            slice_id: count for result in results for slice_id, count in result.slice_counts.items()
        }
        _run_logged_phase(
            "source-stage row-count validation",
            lambda: _validate_source_stage_counts(options, slice_counts),
        )
        replace_connection(options.to_db_key, target_ref)
        stage_tables = _create_target_worker_stages(
            options,
            target_ref,
            stage_state,
            worker_count=write_workers,
        )
        source_by_slice = {
            slice_id: result.table for result in results for slice_id in result.slice_counts
        }
        tasks = deque(
            WholeKeyTask(item, source_by_slice[item.index], slice_counts[item.index])
            for item in options.transfer_slices
        )
        staged_counts = _run_whole_key_writers(
            options,
            stage_state,
            stage_tables,
            tasks,
            insert_retry_cnt=insert_retry_cnt,
        )
        total_rows = sum(slice_counts.values())
        stage_state.expected_source_rows = total_rows
        stage_state.slice_counts = [
            TransferSliceRowCount(
                index=item.index,
                label=format_transfer_slice_log_label(options, item),
                expected_rows=slice_counts[item.index],
                streamed_rows=staged_counts[item.index],
            )
            for item in options.transfer_slices
        ]
        replace_connection(options.to_db_key, target_ref)
        _run_logged_phase(
            "source/stage row-count validation",
            lambda: _validate_target_stages(
                options,
                target_ref,
                stage_state,
                stage_tables,
                slice_counts,
                staged_counts,
            ),
        )
        from .staged_attempt import _consolidate_worker_stages

        _run_logged_phase(
            "worker-stage consolidation",
            lambda: _consolidate_worker_stages(options, target_ref, stage_state, stage_tables),
        )
        validate_loaded_stage_row_count(
            options=options,
            connection_refs=refs,
            stage_state=stage_state,
            total_rows=total_rows,
            open_connection=get_sql_connection,
        )
        _run_logged_phase(
            "target finalization",
            lambda: finalize_loaded_stage(options, refs, stage_state, total_rows),
        )
        return total_rows  # noqa: TRY300
    except Exception as exc:
        error = exc
        raise
    finally:
        try:
            time_print(pipeline_phase_message("stage cleanup"))
            cleanup_stage(
                options,
                refs,
                stage_state,
                options.retry_cnt,
                drop_created_target=error is not None,
            )
            _cleanup_source_stages(options, source_ref, source_stage_tables)
            time_print(pipeline_phase_message("stage cleanup", complete=True))
        except Exception as exc:
            cleanup_error = exc
            if error is None:
                raise
            time_print(f"Cleanup failed while handling transfer error: {exc!r}")
        finally:
            close_connection_ref(source_ref, options.from_db_key, "source")
            close_connection_ref(target_ref, options.to_db_key, "target")
        if cleanup_error is not None and error is None:
            raise cleanup_error


def _prepare_attempt(
    options: TransferOptions,
    refs: TransferConnectionRefs,
    stage_state: TransferStageState,
) -> None:
    representative_sql = (options.transfer_slices or [])[0].source_sql
    source_schema = inspect_source_query_schema(
        options.from_db_backend,
        refs.source["connection"],
        representative_sql,
    )
    source_columns = [column.name for column in source_schema]
    if not source_columns:
        raise ValueError(
            "Source staging requires an inspectable source schema with at least one column."
        )
    stage_state.source_columns = source_columns
    stage_state.source_column_types = {column.name: column.native_type for column in source_schema}
    stage_state.internal_columns = resolve_internal_columns(
        source_columns,
        options.from_db_backend,
        table_schema_names=(options.table_schema or {}).keys(),
    )
    cleanup_superseded_transfer_stages(
        options=options,
        connection=refs.source["connection"],
        backend=options.from_db_backend,
        connection_key=options.from_db_key,
        staging_schema=options.source_transfer_staging_schema,
        internal_columns=stage_state.internal_columns,
    )
    cleanup_superseded_transfer_stages(
        options=options,
        connection=refs.target["connection"],
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
    stage_state.stage_column_types = _with_internal_column_types(source_types, options, stage_state)
    ensure_transfer_target_table(options, refs, stage_state, source_columns)


def _allocate_source_stage_name(
    options: TransferOptions,
    source_ref: dict[str, Any],
    worker_index: int,
) -> str:
    preferred = f"{options.transfer_id}__r{worker_index:05d}"
    for attempt in range(10):
        suffix = (
            preferred
            if attempt == 0
            else collision_stage_suffix(options.from_db_backend, preferred, uuid.uuid4().hex)
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
    raise RuntimeError("Could not allocate a unique source stage table name.")


def _run_source_stage_workers(
    options: TransferOptions,
    stage_state: TransferStageState,
    assignments: list[list[TransferSlice]],
    stage_tables: list[str],
) -> list[SourceStageResult]:
    cancellation = threading.Event()
    results: list[SourceStageResult] = []
    with ThreadPoolExecutor(max_workers=len(assignments)) as executor:
        pending = {
            executor.submit(
                contextvars.copy_context().run,
                _source_stage_worker,
                options,
                stage_state,
                worker_index,
                slices,
                stage_tables[worker_index],
                cancellation,
            )
            for worker_index, slices in enumerate(assignments)
        }
        while pending:
            done, pending = wait(pending, return_when=FIRST_EXCEPTION)
            for future in done:
                exc = future.exception()
                if exc is not None:
                    cancellation.set()
                    for other in pending:
                        other.cancel()
                    raise exc
                results.append(future.result())
    return sorted(results, key=lambda item: item.worker_index)


def _source_stage_worker(
    options: TransferOptions,
    stage_state: TransferStageState,
    worker_index: int,
    slices: list[TransferSlice],
    stage_table: str,
    cancellation: threading.Event,
) -> SourceStageResult:
    source_ref = {"connection": get_sql_connection(options.from_db_key)}
    adapter = get_backend_adapter(options.from_db_backend)
    created = False
    counts: dict[int, int] = {}
    try:
        for position, transfer_slice in enumerate(slices, start=1):
            if cancellation.is_set():
                break
            label = format_transfer_slice_log_label(options, transfer_slice)
            time_print(
                staged_reader_slice_message(
                    worker_index,
                    options.transfer_concurrency.effective_read,
                    position,
                    len(slices),
                    label,
                    "starting",
                    stage_table,
                )
            )
            select_sql = build_snapshot_select_sql(
                backend=options.from_db_backend,
                source_sql=transfer_slice.source_sql,
                source_columns=stage_state.source_columns,
                transfer_id=options.transfer_id or "",
                canonical_destination=options.canonical_destination_identity or "",
                slice_id=transfer_slice.index,
                internal_columns=cast("Any", stage_state.internal_columns),
            )
            snapshot_sql = build_source_snapshot_sql(
                backend=options.from_db_backend,
                snapshot_table=stage_table,
                snapshot_select_sql=select_sql,
                internal_columns=cast("Any", stage_state.internal_columns),
            )
            sql = (
                snapshot_sql.create_sql
                if not created
                else build_append_snapshot_slice_sql(
                    backend=options.from_db_backend,
                    snapshot_table=stage_table,
                    source_columns=stage_state.source_columns,
                    internal_columns=cast("Any", stage_state.internal_columns),
                    snapshot_select_sql=select_sql,
                )
            )
            execute_transfer_materialization(
                adapter,
                options.from_db_backend,
                source_ref["connection"],
                sql,
            )
            created = True
            count = _count_source_slice(
                options,
                source_ref["connection"],
                stage_table,
                transfer_slice.index,
                stage_state,
            )
            counts[transfer_slice.index] = count
            time_print(
                staged_reader_slice_message(
                    worker_index,
                    options.transfer_concurrency.effective_read,
                    position,
                    len(slices),
                    label,
                    f"finished with {count} row(s) in",
                    stage_table,
                )
            )
        if created:
            final_sql = build_source_snapshot_sql(
                backend=options.from_db_backend,
                snapshot_table=stage_table,
                snapshot_select_sql="SELECT 1",
                internal_columns=cast("Any", stage_state.internal_columns),
            )
            for sql in final_sql.post_create_sqls:
                adapter.execute_command(source_ref["connection"], sql)
        return SourceStageResult(worker_index, stage_table, counts)
    finally:
        close_connection_ref(source_ref, options.from_db_key, f"source stage reader {worker_index}")


def _count_source_slice(
    options: TransferOptions,
    connection: Any,
    stage_table: str,
    slice_id: int,
    stage_state: TransferStageState,
) -> int:
    internal = stage_state.internal_columns
    if internal is None:
        raise RuntimeError("Transfer internal columns were not resolved.")
    adapter = get_backend_adapter(options.from_db_backend)
    slice_column = adapter.quote_identifier(internal.slice_id)
    result = _read_backend(
        options.from_db_backend,
        connection,
        f"SELECT COUNT(*) FROM {stage_table} WHERE {slice_column} = {slice_id}",
        print_queries=False,
        output_type="dict",
        action_name="snapshot counting",
        phase="count_snapshot",
    )
    return int(result.columns[0][0])


def _validate_source_stage_counts(options: TransferOptions, counts: dict[int, int]) -> None:
    expected = {item.index for item in options.transfer_slices or []}
    if set(counts) != expected:
        raise RuntimeError(
            "Source stages do not contain exactly one result for every transfer key."
        )


def _create_target_worker_stages(
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
    tables: list[str] = []
    for worker_id in range(worker_count):
        table = create_stage_table(
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
        )
        tables.append(table)
        stage_state.stage_table = tables[0]
        stage_state.stage_tables = list(tables)
        stage_state.stage_table_created = True
    return tables


def _run_whole_key_writers(
    options: TransferOptions,
    stage_state: TransferStageState,
    stage_tables: list[str],
    tasks: deque[WholeKeyTask],
    *,
    insert_retry_cnt: int,
) -> dict[int, int]:
    cancellation = threading.Event()
    task_lock = threading.Lock()
    count_lock = threading.Lock()
    staged_counts: dict[int, int] = {}
    with ThreadPoolExecutor(max_workers=len(stage_tables)) as executor:
        pending = {
            executor.submit(
                contextvars.copy_context().run,
                _whole_key_writer,
                options,
                stage_state,
                worker_index,
                stage_table,
                tasks,
                task_lock,
                staged_counts,
                count_lock,
                cancellation,
                insert_retry_cnt,
            )
            for worker_index, stage_table in enumerate(stage_tables)
        }
        while pending:
            done, pending = wait(pending, return_when=FIRST_EXCEPTION)
            for future in done:
                exc = future.exception()
                if exc is not None:
                    cancellation.set()
                    for other in pending:
                        other.cancel()
                    raise exc
    return staged_counts


def _whole_key_writer(
    options: TransferOptions,
    stage_state: TransferStageState,
    worker_index: int,
    stage_table: str,
    tasks: deque[WholeKeyTask],
    task_lock: threading.Lock,
    staged_counts: dict[int, int],
    count_lock: threading.Lock,
    cancellation: threading.Event,
    insert_retry_cnt: int,
) -> None:
    source_ref: dict[str, Any] = {}
    target_ref: dict[str, Any] = {}
    sizer = AdaptiveBatchSizer(
        enabled=options.adaptive_batch_size,
        current_size=options.batch_size,
        min_size=options.min_batch_size,
        max_size=options.max_batch_size,
        target_seconds=options.target_batch_seconds,
        min_target_seconds=options.min_batch_seconds,
        max_target_seconds=options.max_batch_seconds,
        optimize_by_rows_per_second=options.target_rows_per_second,
        target_rows_per_second_window=options.target_rows_per_second_window,
        target_rows_per_second_deadband=options.target_rows_per_second_deadband,
        adaptive_batch_size_step=options.adaptive_batch_size_step,
        target_memory_bytes=options.target_batch_memory_bytes,
        min_target_memory_bytes=options.min_batch_memory_bytes,
        max_target_memory_bytes=options.max_batch_memory_bytes,
    )
    try:
        source_ref["connection"] = get_sql_connection(options.from_db_key)
        target_ref["connection"] = get_sql_connection(options.to_db_key)
        while not cancellation.is_set():
            with task_lock:
                task = tasks.popleft() if tasks else None
            if task is None:
                return
            transfer_slice = task.transfer_slice
            label = format_transfer_slice_log_label(options, transfer_slice)
            time_print(
                staged_writer_key_message(
                    worker_index,
                    options.transfer_concurrency.effective_write,
                    transfer_slice.index,
                    len(options.transfer_slices or []),
                    label,
                    "starting",
                    stage_table,
                )
            )
            staged = 0
            start_ordinal = 1
            batch_index = 0
            while start_ordinal <= task.row_count:
                batch_index += 1
                stop_ordinal = min(task.row_count + 1, start_ordinal + sizer.current_size)
                batch = _read_key_batch(
                    options,
                    source_ref,
                    task,
                    stage_state,
                    start_ordinal,
                    stop_ordinal,
                )
                expected = stop_ordinal - start_ordinal
                if batch.row_count != expected:
                    message = (
                        f"Source stage key {transfer_slice.index} batch {batch_index} "
                        f"returned {batch.row_count} row(s); expected {expected}."
                    )
                    raise RuntimeError(message)
                started = time.monotonic()
                insert_rows_batch(
                    options.to_db_backend,
                    target_ref,
                    stage_table,
                    batch.columns,
                    batch.rows,
                    retry_fn=run_with_retry,
                    retry_cnt=insert_retry_cnt,
                    timeout_increment=options.timeout_increment,
                    target_column_types=stage_state.stage_column_types,
                    query_label=options.query_label,
                    connection_key=options.to_db_key,
                    rollback_fn=rollback_quietly,
                    replace_connection_fn=replace_connection,
                )
                duration = time.monotonic() - started
                staged += batch.row_count
                sizer.update(
                    duration,
                    inserted_rows=batch.row_count,
                    memory_bytes=batch.approx_memory_bytes(),
                )
                start_ordinal = stop_ordinal
            with count_lock:
                if transfer_slice.index in staged_counts:
                    message = f"Transfer key {transfer_slice.index} was staged twice."
                    raise RuntimeError(message)
                staged_counts[transfer_slice.index] = staged
            time_print(
                staged_writer_key_message(
                    worker_index,
                    options.transfer_concurrency.effective_write,
                    transfer_slice.index,
                    len(options.transfer_slices or []),
                    label,
                    f"finished with {staged} row(s) in",
                    stage_table,
                )
            )
    finally:
        close_connection_ref(source_ref, options.from_db_key, f"source writer {worker_index}")
        close_connection_ref(target_ref, options.to_db_key, f"target writer {worker_index}")


def _read_key_batch(
    options: TransferOptions,
    source_ref: dict[str, Any],
    task: WholeKeyTask,
    stage_state: TransferStageState,
    start_ordinal: int,
    stop_ordinal: int,
) -> RowBatch:
    from .staged_attempt import _read_snapshot_range

    def read_once(_attempt: int) -> RowBatch:
        try:
            return _read_snapshot_range(
                options,
                source_ref["connection"],
                task.source_stage,
                stage_state.source_columns,
                stage_state,
                OrdinalRange(task.transfer_slice.index, start_ordinal, stop_ordinal),
            )
        except Exception:
            rollback_quietly(source_ref["connection"])
            replace_connection(options.from_db_key, source_ref)
            raise

    return cast(
        "RowBatch",
        run_with_retry(
            operation_name=(
                f"reading staged key {task.transfer_slice.index} rows "
                f"{start_ordinal}:{stop_ordinal}"
            ),
            retry_cnt=options.retry_cnt,
            timeout_increment=options.timeout_increment,
            operation=read_once,
            retryable_exceptions=(Exception,),
        ),
    )


def _validate_target_stages(
    options: TransferOptions,
    target_ref: dict[str, Any],
    stage_state: TransferStageState,
    stage_tables: list[str],
    source_counts: dict[int, int],
    staged_counts: dict[int, int],
) -> None:
    if staged_counts != source_counts:
        message = (
            "Source/stage keyed row-count mismatch: "
            f"source={source_counts}, staged={staged_counts}."
        )
        raise RuntimeError(message)
    internal = stage_state.internal_columns
    if internal is None:
        raise RuntimeError("Transfer internal columns were not resolved.")
    validate_transfer_stage_identity(
        options=options,
        connection=target_ref["connection"],
        stage_tables=stage_tables,
        internal_columns=internal,
        expected_slice_counts=source_counts,
    )


def _cleanup_source_stages(
    options: TransferOptions,
    source_ref: dict[str, Any],
    stage_tables: list[str],
) -> None:
    first_error: Exception | None = None
    for stage_table in stage_tables:
        try:
            cleanup_stage_table(
                options.from_db_backend,
                source_ref["connection"],
                stage_table,
                query_label=options.query_label,
            )
        except Exception as exc:  # noqa: BLE001, PERF203
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


def _run_logged_phase(name: str, operation: Any) -> None:
    time_print(pipeline_phase_message(name))
    operation()
    time_print(pipeline_phase_message(name, complete=True))
