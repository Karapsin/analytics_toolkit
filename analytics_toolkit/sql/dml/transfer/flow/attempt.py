from __future__ import annotations

from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import replace
from typing import Any
import uuid

import pandas as pd
from tqdm import tqdm

from ....clickhouse.options import validate_ch_columns_in_columns
from ....connection.get_sql_connection import get_sql_connection
from ....ddl.schema import validate_table_schema_columns
from analytics_toolkit.general import time_print
from ...load.stage import create_stage_table
from ...load.load_sql_table import insert_rows_batch
from ...table._basic_ops import insert_from_table
from ...table.table_validation import validate_key_columns_in_columns
from .estimate import estimate_source_rows
from .finalize import (
    cleanup_stage,
    finalize_loaded_stage,
)
from .parquet_stage import (
    create_parquet_stage_table,
    ensure_parquet_staging_dependencies,
    infer_trino_column_types_from_rows,
    parquet_row_group_size,
    sample_dataframe_from_batch,
    write_batch_to_parquet_stage,
)
from ..runtime.models import (
    AdaptiveBatchSizer,
    RowBatch,
    TransferConnectionRefs,
    TransferOptions,
    TransferSlice,
    TransferStageState,
    make_gp_insert_chunk_sizer,
)
from ..runtime.retry import close_connection_ref, run_with_retry
from ..io.source import iter_source_batches
from ..schema import inspect_source_query_schema, map_source_schema_to_target
from ....execution.operation_runner import _format_duration
from .stage import create_stage_state, initialize_stage_for_first_batch

_TRANSFER_PROGRESS_UNKNOWN_TOTAL_FORMAT = (
    "{desc}: {n_pretty}{unit} [{elapsed}, {rate_fmt}{postfix}]"
)
_TRANSFER_PROGRESS_TOTAL_FORMAT = (
    "{l_bar}{bar}| {n_pretty}/{total_pretty} "
    "[{elapsed}<{remaining}, {rate_fmt}{postfix}]"
)


def run_transfer_attempt(
    options: TransferOptions,
    read_retry_cnt: int,
    insert_retry_cnt: int,
) -> int:
    if options.transfer_slices is not None:
        return run_keyed_transfer_attempt(
            options=options,
            read_retry_cnt=read_retry_cnt,
            insert_retry_cnt=insert_retry_cnt,
        )

    connection_refs = TransferConnectionRefs(
        source={"connection": get_sql_connection(options.from_db_key)},
        target={"connection": get_sql_connection(options.to_db_key)},
    )
    total_rows = 0
    transfer_error: Exception | None = None
    cleanup_error: Exception | None = None
    stage_state = create_stage_state(options, connection_refs)

    try:
        source_schema = inspect_source_query_schema(
            options.from_db_backend,
            connection_refs.source["connection"],
            options.source_sql,
        )
        stage_state.source_column_types = {
            column.name: column.native_type for column in source_schema
        }
        if options.table_schema is not None and source_schema:
            stage_state.stage_column_types = validate_table_schema_columns(
                options.table_schema,
                [column.name for column in source_schema],
            )
        elif options.table_schema is not None:
            stage_state.stage_column_types = options.table_schema
        elif source_schema:
            stage_state.stage_column_types = map_source_schema_to_target(
                source_schema,
                options.to_db_backend,
            )
        total_rows = load_stage_batches(
            options=options,
            connection_refs=connection_refs,
            stage_state=stage_state,
            read_retry_cnt=read_retry_cnt,
            insert_retry_cnt=insert_retry_cnt,
        )
        finalize_loaded_stage(
            options=options,
            connection_refs=connection_refs,
            stage_state=stage_state,
            total_rows=total_rows,
        )
    except Exception as exc:
        transfer_error = exc
    finally:
        try:
            cleanup_stage(
                options=options,
                connection_refs=connection_refs,
                stage_state=stage_state,
                read_retry_cnt=read_retry_cnt,
            )
        except Exception as exc:
            cleanup_error = exc
        finally:
            close_connection_ref(connection_refs.source, options.from_db_key, "source")
            close_connection_ref(connection_refs.target, options.to_db_key, "target")

    if transfer_error is not None:
        if cleanup_error is not None:
            time_print(
                f"Cleanup failed while handling transfer error: {cleanup_error!r}"
            )
        raise transfer_error.with_traceback(transfer_error.__traceback__)
    if cleanup_error is not None:
        raise cleanup_error.with_traceback(cleanup_error.__traceback__)
    return total_rows


def run_keyed_transfer_attempt(
    options: TransferOptions,
    read_retry_cnt: int,
    insert_retry_cnt: int,
) -> int:
    if options.transfer_slices is None:
        raise ValueError("run_keyed_transfer_attempt requires transfer_slices.")

    connection_refs = TransferConnectionRefs(
        source={"connection": get_sql_connection(options.from_db_key)},
        target={"connection": get_sql_connection(options.to_db_key)},
    )
    total_rows = 0
    transfer_error: Exception | None = None
    cleanup_error: Exception | None = None
    stage_state = create_stage_state(options, connection_refs)
    worker_stage_states: list[WorkerStageState] | None = None

    try:
        representative_sql = options.transfer_slices[0].source_sql
        source_schema = inspect_source_query_schema(
            options.from_db_backend,
            connection_refs.source["connection"],
            representative_sql,
        )
        stage_state.source_column_types = {
            column.name: column.native_type for column in source_schema
        }
        initialize_shared_stage_for_keyed_slices(
            options=options,
            connection_refs=connection_refs,
            stage_state=stage_state,
            source_schema=source_schema,
        )
        worker_stage_states = build_keyed_worker_stage_states(
            options=options,
            stage_state=stage_state,
        )
        total_rows = load_keyed_stage_slices(
            options=options,
            worker_stage_states=worker_stage_states,
            read_retry_cnt=read_retry_cnt,
            insert_retry_cnt=insert_retry_cnt,
        )
        consolidate_keyed_worker_stages(
            options=options,
            connection_refs=connection_refs,
            worker_stage_states=worker_stage_states,
            stage_state=stage_state,
        )
        finalize_loaded_stage(
            options=options,
            connection_refs=connection_refs,
            stage_state=stage_state,
            total_rows=total_rows,
        )
    except Exception as exc:
        transfer_error = exc
    finally:
        try:
            cleanup_stage(
                options=options,
                connection_refs=connection_refs,
                stage_state=stage_state,
                read_retry_cnt=read_retry_cnt,
            )
        except Exception as exc:
            cleanup_error = exc
        finally:
            close_connection_ref(connection_refs.source, options.from_db_key, "source")
            close_connection_ref(connection_refs.target, options.to_db_key, "target")

    if transfer_error is not None:
        if cleanup_error is not None:
            time_print(
                f"Cleanup failed while handling transfer error: {cleanup_error!r}"
            )
        raise transfer_error.with_traceback(transfer_error.__traceback__)
    if cleanup_error is not None:
        raise cleanup_error.with_traceback(cleanup_error.__traceback__)
    return total_rows


class WorkerStageState:
    def __init__(
        self,
        *,
        worker_index: int,
        stage_state: TransferStageState,
        transfer_slices: list[TransferSlice],
    ) -> None:
        self.worker_index = worker_index
        self.stage_state = stage_state
        self.transfer_slices = transfer_slices


def initialize_shared_stage_for_keyed_slices(
    *,
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    source_schema: list[Any],
) -> None:
    source_columns = [column.name for column in source_schema]
    if options.table_schema is not None and source_schema:
        stage_state.stage_column_types = validate_table_schema_columns(
            options.table_schema,
            source_columns,
        )
    elif options.table_schema is not None:
        stage_state.stage_column_types = options.table_schema
        source_columns = list(options.table_schema)
    elif source_schema:
        stage_state.stage_column_types = map_source_schema_to_target(
            source_schema,
            options.to_db_backend,
        )
    else:
        raise ValueError(
            "Keyed transfer requires table_schema or an inspectable source query "
            "schema so the shared stage table can be created before workers start."
        )

    if not source_columns:
        raise ValueError("Keyed transfer source schema must contain at least one column.")

    validate_key_columns_in_columns(
        options.key_columns,
        source_columns,
    )
    validate_key_columns_in_columns(
        options.gp_distributed_by_key,
        source_columns,
    )
    validate_ch_columns_in_columns(
        options.partition_by,
        source_columns,
        "partition_by",
        data_name="staged data",
    )
    validate_ch_columns_in_columns(
        options.order_by,
        source_columns,
        "order_by",
        data_name="staged data",
    )

    sample_batch = pd.DataFrame(columns=source_columns)
    stage_state.first_non_empty_batch = sample_batch
    if options.trino_mode == "parquet":
        create_parquet_stage_table(
            options=options,
            connection_refs=connection_refs,
            stage_state=stage_state,
        )
        return

    if _uses_keyed_worker_stages(options):
        stage_tables = _create_keyed_worker_stage_tables(
            options=options,
            connection_refs=connection_refs,
            stage_state=stage_state,
            sample_batch=sample_batch,
            column_types=stage_state.stage_column_types,
        )
        stage_state.stage_table = stage_tables[0]
        stage_state.stage_tables = stage_tables
        stage_state.stage_table_created = True
        _commit_if_supported(connection_refs.target["connection"])
        return

    stage_state.stage_table = create_stage_table(
        connection_type=options.to_db_backend,
        connection=connection_refs.target["connection"],
        target_table=options.target_table,
        batch=sample_batch,
        column_types=stage_state.stage_column_types,
        gp_distributed_by_key=options.gp_distributed_by_key,
        connection_key=options.to_db_key,
        query_label=options.query_label,
        transfer_staging_schema=options.transfer_staging_schema,
        transfer_staging_username=options.transfer_staging_username,
    )
    stage_state.stage_table_created = True


def _uses_keyed_worker_stages(options: TransferOptions) -> bool:
    return (
        options.transfer_slices is not None
        and options.trino_mode != "parquet"
        and options.concurrency > 1
        and len(options.transfer_slices) > 1
    )


def _create_keyed_worker_stage_tables(
    *,
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    sample_batch: pd.DataFrame,
    column_types: dict[str, str] | None,
) -> list[str]:
    transfer_slices = options.transfer_slices or []
    worker_count = min(options.concurrency, len(transfer_slices))
    run_token = uuid.uuid4().hex[:8]
    stage_tables: list[str] = []
    for worker_index in range(worker_count):
        stage_table = create_stage_table(
            connection_type=options.to_db_backend,
            connection=connection_refs.target["connection"],
            target_table=options.target_table,
            batch=sample_batch,
            column_types=column_types,
            gp_distributed_by_key=options.gp_distributed_by_key,
            connection_key=options.to_db_key,
            query_label=options.query_label,
            transfer_staging_schema=options.transfer_staging_schema,
            transfer_staging_username=options.transfer_staging_username,
            random_suffix=f"{run_token}__w{worker_index:05d}",
        )
        stage_tables.append(stage_table)
        stage_state.stage_table = stage_tables[0]
        stage_state.stage_tables = list(stage_tables)
        stage_state.stage_table_created = True
    return stage_tables


def _commit_if_supported(connection: Any) -> None:
    commit = getattr(connection, "commit", None)
    if callable(commit):
        commit()


def build_keyed_worker_stage_states(
    *,
    options: TransferOptions,
    stage_state: TransferStageState,
) -> list[WorkerStageState]:
    transfer_slices = options.transfer_slices or []
    stage_tables = stage_state.stage_tables or (
        [stage_state.stage_table] if stage_state.stage_table is not None else []
    )
    worker_count = len(stage_tables)
    if worker_count == 0:
        raise RuntimeError("Expected stage table to be initialized.")
    return [
        WorkerStageState(
            worker_index=worker_index,
            stage_state=_copy_stage_state_for_worker(
                stage_state,
                stage_table=stage_tables[worker_index],
            ),
            transfer_slices=transfer_slices[worker_index::worker_count],
        )
        for worker_index in range(worker_count)
    ]


def _copy_stage_state_for_worker(
    stage_state: TransferStageState,
    *,
    stage_table: str,
) -> TransferStageState:
    return TransferStageState(
        target_exists=stage_state.target_exists,
        stage_table_created=stage_state.stage_table_created,
        first_non_empty_batch=stage_state.first_non_empty_batch,
        source_column_types=stage_state.source_column_types,
        stage_column_types=stage_state.stage_column_types,
        insert_column_types=stage_state.insert_column_types,
        stage_table=stage_table,
        stage_tables=[stage_table],
        stage_external_location=stage_state.stage_external_location,
    )


def load_keyed_stage_slices(
    *,
    options: TransferOptions,
    worker_stage_states: list[WorkerStageState],
    read_retry_cnt: int,
    insert_retry_cnt: int,
) -> int:
    total_rows = 0
    with ThreadPoolExecutor(max_workers=len(worker_stage_states)) as executor:
        pending = {
            executor.submit(
                load_keyed_stage_worker,
                options=options,
                worker_stage_state=worker_stage_state,
                read_retry_cnt=read_retry_cnt,
                insert_retry_cnt=insert_retry_cnt,
            )
            for worker_stage_state in worker_stage_states
        }
        while pending:
            done, pending = wait(pending, return_when=FIRST_EXCEPTION)
            for future in done:
                exc = future.exception()
                if exc is not None:
                    for pending_future in pending:
                        pending_future.cancel()
                    raise exc
                total_rows += future.result()
    return total_rows


def load_keyed_stage_worker(
    *,
    options: TransferOptions,
    worker_stage_state: WorkerStageState,
    read_retry_cnt: int,
    insert_retry_cnt: int,
) -> int:
    connection_refs = TransferConnectionRefs(
        source={"connection": get_sql_connection(options.from_db_key)},
        target={"connection": get_sql_connection(options.to_db_key)},
    )
    total_rows = 0
    try:
        for transfer_slice in worker_stage_state.transfer_slices:
            worker_options = replace(options, source_sql=transfer_slice.source_sql)
            total_rows += load_stage_batches(
                options=worker_options,
                connection_refs=connection_refs,
                stage_state=worker_stage_state.stage_state,
                read_retry_cnt=read_retry_cnt,
                insert_retry_cnt=insert_retry_cnt,
                slice_index=transfer_slice.index,
            )
        return total_rows
    finally:
        close_connection_ref(connection_refs.source, options.from_db_key, "source")
        close_connection_ref(connection_refs.target, options.to_db_key, "target")


def consolidate_keyed_worker_stages(
    *,
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    worker_stage_states: list[WorkerStageState],
    stage_state: TransferStageState,
) -> None:
    if len(worker_stage_states) <= 1:
        return
    aggregate_stage_table = stage_state.stage_table
    if aggregate_stage_table is None:
        raise RuntimeError("Expected aggregate stage table to be initialized.")
    for worker_stage_state in worker_stage_states[1:]:
        worker_stage_table = worker_stage_state.stage_state.stage_table
        if worker_stage_table is None:
            raise RuntimeError("Expected worker stage table to be initialized.")
        insert_from_table(
            options.to_db_backend,
            connection_refs.target["connection"],
            aggregate_stage_table,
            worker_stage_table,
            column_types=stage_state.stage_column_types,
            query_label=options.query_label,
        )


def load_stage_batches(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    read_retry_cnt: int,
    insert_retry_cnt: int,
    slice_index: int | None = None,
) -> int:
    if options.trino_mode == "parquet":
        return load_parquet_stage_batches(
            options=options,
            connection_refs=connection_refs,
            stage_state=stage_state,
            read_retry_cnt=read_retry_cnt,
            slice_index=slice_index,
        )

    total_rows = 0
    estimated_total_rows = None
    if options.progress and options.estimate_total_rows:
        estimated_total_rows = estimate_source_rows(
            options,
            connection_refs.source["connection"],
        )
    progress_bar = _make_transfer_progress_bar(options, total=estimated_total_rows)
    progress_tracker = _ProgressTracker(progress_bar)
    batch_sizer = AdaptiveBatchSizer(
        enabled=options.adaptive_batch_size,
        current_size=options.batch_size,
        min_size=options.min_batch_size,
        max_size=options.max_batch_size,
        min_target_seconds=options.min_batch_seconds,
        max_target_seconds=options.max_batch_seconds,
        optimize_by_rows_per_second=options.target_rows_per_second,
        target_seconds=options.target_batch_seconds,
        target_rows_per_second_window=options.target_rows_per_second_window,
        target_rows_per_second_deadband=options.target_rows_per_second_deadband,
        adaptive_batch_size_step=options.adaptive_batch_size_step,
        target_memory_bytes=options.target_batch_memory_bytes,
        min_target_memory_bytes=options.min_batch_memory_bytes,
        max_target_memory_bytes=options.max_batch_memory_bytes,
    )
    gp_insert_chunk_sizer = (
        make_gp_insert_chunk_sizer(options)
        if options.to_db_backend == "gp"
        else None
    )
    try:
        for batch in iter_source_batches(
            options.from_db_key,
            options.from_db_backend,
            connection_refs.source,
            options.source_sql,
            options.batch_size,
            retry_cnt=read_retry_cnt,
            timeout_increment=options.timeout_increment,
            query_label=options.query_label,
            get_batch_size=lambda: batch_sizer.current_size,
        ):
            if batch.empty:
                continue

            if stage_state.first_non_empty_batch is None:
                initialize_stage_for_first_batch(
                    options=options,
                    connection_refs=connection_refs,
                    stage_state=stage_state,
                    batch=batch,
                )

            progress_tracker.start_batch()
            batch_memory_bytes = (
                batch.approx_memory_bytes()
                if options.target_batch_memory_bytes is not None
                else None
            )

            current_batch_duration_seconds = 0.0

            def update_batch_sizer(
                duration_seconds: float,
                inserted_rows: int,
            ) -> None:
                nonlocal current_batch_duration_seconds
                current_batch_duration_seconds = duration_seconds
                batch_sizer.update(
                    duration_seconds,
                    inserted_rows=inserted_rows,
                    memory_bytes=batch_memory_bytes,
                )

            def update_gp_insert_chunk_sizer(
                duration_seconds: float,
                inserted_rows: int,
            ) -> None:
                if gp_insert_chunk_sizer is None:
                    return
                gp_insert_chunk_sizer.update(
                    duration_seconds,
                    inserted_rows=inserted_rows,
                )

            inserted_rows = insert_rows_batch(
                options.to_db_backend,
                connection_refs.target,
                stage_state.stage_table,
                batch.columns,
                batch.rows,
                retry_fn=run_with_retry,
                retry_cnt=insert_retry_cnt,
                timeout_increment=options.timeout_increment,
                target_column_types=stage_state.stage_column_types,
                gp_insert_chunk_size=options.gp_insert_chunk_size,
                trino_insert_chunk_size=options.trino_insert_chunk_size,
                query_label=options.query_label,
                on_success=update_batch_sizer,
                on_progress=progress_tracker.update,
                gp_insert_page_size_getter=(
                    (lambda: gp_insert_chunk_sizer.current_size)
                    if gp_insert_chunk_sizer is not None
                    else None
                ),
                on_gp_insert_page_success=(
                    update_gp_insert_chunk_sizer
                    if gp_insert_chunk_sizer is not None
                    else None
                ),
            )
            progress_tracker.complete_batch(inserted_rows)
            total_rows += inserted_rows
            rows_per_second = (
                inserted_rows / current_batch_duration_seconds
                if current_batch_duration_seconds > 0
                else None
            )
            rows_per_second_text = (
                f"{rows_per_second:,.2f}"
                if rows_per_second is not None
                else "N/A"
            )
            time_print(
                f"Transferred batch of "
                f"{_format_transfer_progress_count(inserted_rows)} row(s) "
                f"to {stage_state.stage_table} in "
                f"{_format_duration(current_batch_duration_seconds)} "
                f"({rows_per_second_text} row/s); total transferred "
                f"{_format_transfer_progress_count(total_rows)} row(s)",
                connection=options.to_db_key,
                backend=options.to_db_backend,
            )
        return total_rows
    finally:
        progress_bar.close()


def load_parquet_stage_batches(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    read_retry_cnt: int,
    slice_index: int | None = None,
) -> int:
    pa, pq, fsspec_module = ensure_parquet_staging_dependencies()
    total_rows = 0
    file_index = 0
    row_group_size = parquet_row_group_size(options)
    estimated_total_rows = None
    if options.progress and options.estimate_total_rows:
        estimated_total_rows = estimate_source_rows(
            options,
            connection_refs.source["connection"],
        )
    progress_bar = _make_transfer_progress_bar(options, total=estimated_total_rows)
    progress_tracker = _ProgressTracker(progress_bar)
    try:
        for batch in iter_source_batches(
            options.from_db_key,
            options.from_db_backend,
            connection_refs.source,
            options.source_sql,
            row_group_size,
            retry_cnt=read_retry_cnt,
            timeout_increment=options.timeout_increment,
            query_label=options.query_label,
            get_batch_size=lambda: row_group_size,
        ):
            if batch.empty:
                continue

            if stage_state.first_non_empty_batch is None:
                _initialize_parquet_stage_for_first_batch(
                    options=options,
                    connection_refs=connection_refs,
                    stage_state=stage_state,
                    batch=batch,
                )

            if stage_state.stage_external_location is None:
                raise RuntimeError("Expected Parquet stage location to be initialized.")

            progress_tracker.start_batch()
            inserted_rows = write_batch_to_parquet_stage(
                batch,
                file_index=file_index,
                slice_index=slice_index,
                stage_external_location=stage_state.stage_external_location,
                pa=pa,
                pq=pq,
                fsspec_module=fsspec_module,
                row_group_size=row_group_size,
            )
            file_index += 1
            progress_tracker.update(inserted_rows)
            progress_tracker.complete_batch(inserted_rows)
            total_rows += inserted_rows
            time_print(
                f"Wrote Parquet transfer batch of "
                f"{_format_transfer_progress_count(inserted_rows)} row(s) "
                f"to {stage_state.stage_external_location}; total transferred "
                f"{_format_transfer_progress_count(total_rows)} row(s)",
                connection=options.to_db_key,
                backend=options.to_db_backend,
            )
            del batch
        return total_rows
    finally:
        progress_bar.close()


def _initialize_parquet_stage_for_first_batch(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    batch: RowBatch,
) -> None:
    if options.table_schema is not None:
        stage_state.stage_column_types = validate_table_schema_columns(
            options.table_schema,
            batch.columns,
        )
    elif stage_state.stage_column_types is None:
        stage_state.stage_column_types = infer_trino_column_types_from_rows(batch)

    validate_key_columns_in_columns(
        options.key_columns,
        batch.columns,
    )
    stage_state.first_non_empty_batch = sample_dataframe_from_batch(batch)
    create_parquet_stage_table(
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
    )


class _ProgressTracker:
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


def _make_transfer_progress_bar(options: TransferOptions, *, total: int | None) -> Any:
    progress_cls = _make_transfer_progress_bar_class(tqdm)
    bar_format = (
        _TRANSFER_PROGRESS_TOTAL_FORMAT
        if total is not None
        else _TRANSFER_PROGRESS_UNKNOWN_TOTAL_FORMAT
    )
    return progress_cls(
        total=total,
        desc=f"transfer_table {options.to_db_key}.{options.target_table}",
        unit="row",
        disable=not options.progress,
        bar_format=bar_format,
    )


def _make_transfer_progress_bar_class(base_tqdm: Any) -> Any:
    class _TransferProgressTqdm(base_tqdm):
        @property
        def format_dict(self) -> dict[str, Any]:
            format_dict = super().format_dict
            total = format_dict.get("total")
            format_dict["n_pretty"] = _format_transfer_progress_count(
                format_dict.get("n", self.n)
            )
            format_dict["total_pretty"] = (
                "?" if total is None else _format_transfer_progress_count(total)
            )
            return format_dict

    return _TransferProgressTqdm


def _format_transfer_progress_count(value: Any) -> str:
    return f"{value:_}"
