from __future__ import annotations

# ruff: noqa: C901

import contextvars
import uuid
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import replace
from typing import Any

import pandas as pd
from tqdm import tqdm

from analytics_toolkit.general import time_print

from ....backends import get_backend_adapter
from ....connection.get_sql_connection import get_sql_connection
from ....ddl.schema import validate_table_schema_columns
from ....execution.operation_runner import _format_duration
from ...load.load_sql_table import insert_rows_batch
from ...load.stage import create_stage_table
from ...table._basic_ops import insert_from_table
from ...table.table_validation import (
    validate_key_columns_in_columns,
    validate_upsert_partition_column_in_columns,
)
from ..io.source import iter_source_batches
from ..runtime.models import (
    AdaptiveBatchSizer,
    RowBatch,
    TransferConnectionRefs,
    TransferOptions,
    TransferStageState,
)
from ..runtime.retry import (
    close_connection_ref,
    replace_connection,
    rollback_quietly,
    run_with_fresh_connection,
    run_with_retry,
)
from ..schema import (
    inspect_source_query_schema,
    map_source_schema_to_target,
)
from ....backends.source_estimate import estimate_source_rows
from .finalize import cleanup_stage, finalize_loaded_stage
from .finalize import cleanup_transfer_attempt_stages as cleanup_attempt_stages
from .keyed import WorkerStageState, build_keyed_worker_stage_states
from .keyed_pipeline import run_keyed_transfer_pipeline
from .keyed_phases import finish_keyed_transfer
from .logging import (
    ProgressTracker,
    format_transfer_key_log_fragment,
    format_transfer_slice_log_label,
)
from .parquet_stage import (
    create_parquet_stage_table,
    ensure_parquet_staging_dependencies,
    parquet_row_group_size,
    write_batch_to_parquet_stage,
)
from .parquet_batches import (
    append_transfer_identity_columns as _append_transfer_identity_columns,
    initialize_parquet_stage_for_first_batch as _initialize_parquet_stage_for_first_batch_impl,
    load_parquet_stage_batches as _load_parquet_stage_batches_impl,
)
from .progress import format_transfer_progress_count, make_transfer_progress_bar
from .row_counts import (
    cleanup_materialized_sources as cleanup_sources,
    cleanup_sources_and_close,
    disable_query_limit_for_transfer_reads,
    prepare_row_count_validated_options,
    validate_loaded_stage_row_count,
    validate_slice_row_count,
    validate_streamed_row_count,
)
from .stage import (
    _with_internal_column_types,
    create_stage_state,
    ensure_transfer_target_table,
    initialize_stage_for_first_batch,
)
from .stage_identity import resolve_internal_columns
from .staged_attempt import run_staged_source_transfer_attempt
from .superseded import cleanup_superseded_transfer_stages


def run_transfer_attempt(
    options: TransferOptions,
    read_retry_cnt: int,
    insert_retry_cnt: int,
) -> int:
    if options.source_transfer_staging_schema is not None:
        return run_staged_source_transfer_attempt(
            options,
            insert_retry_cnt=insert_retry_cnt,
        )
    if options.transfer_slices is not None:
        return run_keyed_transfer_attempt(
            options=options,
            read_retry_cnt=read_retry_cnt,
            insert_retry_cnt=insert_retry_cnt,
        )

    connection_refs = TransferConnectionRefs(
        source={"connection": get_sql_connection(options.from_db_key)},
        target={},
    )
    total_rows = 0
    transfer_error: Exception | None = None
    cleanup_error: Exception | None = None
    stage_state = _run_with_fresh_target_connection(
        options,
        "target_state",
        lambda target_ref: create_stage_state(
            options,
            TransferConnectionRefs(
                source=connection_refs.source,
                target=target_ref,
            ),
        ),
    )

    try:
        source_schema = inspect_source_query_schema(
            options.from_db_backend,
            connection_refs.source["connection"],
            options.source_sql,
        )
        stage_state.source_column_types = {
            column.name: column.native_type for column in source_schema
        }
        stage_state.source_columns = [column.name for column in source_schema]
        stage_state.internal_columns = resolve_internal_columns(
            stage_state.source_columns,
            options.from_db_backend,
            table_schema_names=(options.table_schema or {}).keys(),
        )
        _cleanup_target_superseded_stages(options, stage_state)
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
                source_backend=options.from_db_backend,
            )
        _run_with_fresh_target_connection(
            options,
            "prepare_target",
            lambda target_ref: ensure_transfer_target_table(
                options,
                TransferConnectionRefs(
                    source=connection_refs.source,
                    target=target_ref,
                ),
                stage_state,
                [column.name for column in source_schema],
            ),
        )
        load_options = prepare_row_count_validated_options(
            options=options,
            connection_refs=connection_refs,
            stage_state=stage_state,
        )
        total_rows = load_stage_batches(
            options=load_options,
            connection_refs=connection_refs,
            stage_state=stage_state,
            read_retry_cnt=read_retry_cnt,
            insert_retry_cnt=insert_retry_cnt,
        )
        validate_loaded_stage_row_count(
            options=options,
            connection_refs=connection_refs,
            stage_state=stage_state,
            total_rows=total_rows,
            open_connection=get_sql_connection,
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
        cleanup_error = cleanup_attempt_stages(
            options,
            connection_refs,
            stage_state,
            read_retry_cnt,
            transfer_error,
            cleanup_stage,
        )
        close_connection_ref(connection_refs.source, options.from_db_key, "source")

    if transfer_error is not None:
        if cleanup_error is not None:
            time_print(f"Cleanup failed while handling transfer error: {cleanup_error!r}")
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
        target={},
    )
    total_rows = 0
    transfer_error: Exception | None = None
    cleanup_error: Exception | None = None
    stage_state = _run_with_fresh_target_connection(
        options,
        "target_state",
        lambda target_ref: create_stage_state(
            options,
            TransferConnectionRefs(
                source=connection_refs.source,
                target=target_ref,
            ),
        ),
    )
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
        stage_state.source_columns = [column.name for column in source_schema]
        stage_state.internal_columns = resolve_internal_columns(
            stage_state.source_columns,
            options.from_db_backend,
            table_schema_names=(options.table_schema or {}).keys(),
        )
        _cleanup_target_superseded_stages(options, stage_state)
        _run_with_fresh_target_connection(
            options,
            "create_stage",
            lambda target_ref: initialize_shared_stage_for_keyed_slices(
                options=options,
                connection_refs=TransferConnectionRefs(
                    source=connection_refs.source,
                    target=target_ref,
                ),
                stage_state=stage_state,
                source_schema=source_schema,
            ),
        )
        stage_state.transfer_slices = options.transfer_slices
        worker_stage_states = build_keyed_worker_stage_states(stage_state=stage_state)
        stage_state.worker_stage_states = worker_stage_states
        total_rows = load_keyed_stage_slices(
            options=options,
            stage_state=stage_state,
            worker_stage_states=worker_stage_states,
            read_retry_cnt=read_retry_cnt,
            insert_retry_cnt=insert_retry_cnt,
        )
        finish_keyed_transfer(
            options=options,
            connection_refs=connection_refs,
            worker_stage_states=worker_stage_states,
            stage_state=stage_state,
            total_rows=total_rows,
            open_connection=get_sql_connection,
            consolidate=consolidate_keyed_worker_stages,
            validate=validate_loaded_stage_row_count,
            finalize=finalize_loaded_stage,
        )
    except Exception as exc:
        transfer_error = exc
    finally:
        try:
            if transfer_error is None:
                time_print("Starting keyed transfer pipeline stage cleanup")
            cleanup_stage(
                options=options,
                connection_refs=connection_refs,
                stage_state=stage_state,
                read_retry_cnt=read_retry_cnt,
                drop_created_target=transfer_error is not None,
            )
            if transfer_error is None:
                time_print("Completed keyed transfer pipeline stage cleanup")
        except Exception as exc:
            cleanup_error = exc
        finally:
            close_connection_ref(connection_refs.source, options.from_db_key, "source")

    if transfer_error is not None:
        if cleanup_error is not None:
            time_print(f"Cleanup failed while handling transfer error: {cleanup_error!r}")
        raise transfer_error.with_traceback(transfer_error.__traceback__)
    if cleanup_error is not None:
        raise cleanup_error.with_traceback(cleanup_error.__traceback__)
    return total_rows


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
            source_backend=options.from_db_backend,
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
    validate_upsert_partition_column_in_columns(
        options.upsert_partition_column,
        source_columns,
    )
    validate_key_columns_in_columns(
        options.gp_distributed_by_key,
        source_columns,
    )
    get_backend_adapter(options.to_db_backend).validate_ch_columns_in_columns(
        options.partition_by,
        source_columns,
        "partition_by",
        data_name="staged data",
    )
    get_backend_adapter(options.to_db_backend).validate_ch_columns_in_columns(
        options.order_by,
        source_columns,
        "order_by",
        data_name="staged data",
    )
    ensure_transfer_target_table(
        options,
        connection_refs,
        stage_state,
        source_columns,
    )

    stage_state.source_columns = list(source_columns)
    stage_state.stage_column_types = _with_internal_column_types(
        stage_state.stage_column_types,
        options,
        stage_state,
    )
    stage_columns = [
        *source_columns,
        *(stage_state.internal_columns.names() if stage_state.internal_columns else ()),
    ]
    sample_batch = pd.DataFrame(columns=stage_columns)
    stage_state.first_non_empty_batch = pd.DataFrame(columns=source_columns)
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
        and options.transfer_concurrency.effective_write > 1
        and len(options.transfer_slices) > 1
    )


def _run_with_fresh_target_connection(
    options: TransferOptions,
    role: str,
    operation: Any,
) -> Any:
    return run_with_fresh_connection(
        options.to_db_key,
        role,
        operation,
        open_connection=get_sql_connection,
    )


def _cleanup_target_superseded_stages(
    options: TransferOptions,
    stage_state: TransferStageState,
) -> None:
    if stage_state.internal_columns is None:
        return
    _run_with_fresh_target_connection(
        options,
        "cleanup_superseded_stages",
        lambda target_ref: cleanup_superseded_transfer_stages(
            options=options,
            connection=target_ref["connection"],
            backend=options.to_db_backend,
            connection_key=options.to_db_key,
            staging_schema=options.transfer_staging_schema,
            internal_columns=stage_state.internal_columns,
        ),
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
    worker_count = min(
        options.transfer_concurrency.effective_write,
        len(transfer_slices),
    )
    run_token = options.transfer_id or uuid.uuid4().hex[:8]
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
            destination_hash=options.destination_hash,
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


def load_keyed_stage_slices(
    *,
    options: TransferOptions,
    worker_stage_states: list[WorkerStageState],
    read_retry_cnt: int,
    insert_retry_cnt: int,
    stage_state: TransferStageState | None = None,
) -> int:
    if stage_state is not None:
        return run_keyed_transfer_pipeline(
            options=options,
            stage_state=stage_state,
            writer_stage_states=worker_stage_states,
            read_retry_cnt=read_retry_cnt,
            insert_retry_cnt=insert_retry_cnt,
        )
    total_rows = 0
    with ThreadPoolExecutor(max_workers=len(worker_stage_states)) as executor:
        pending = {
            executor.submit(
                _run_in_context,
                contextvars.copy_context(),
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


def _run_in_context(
    context: contextvars.Context,
    function: Any,
    **kwargs: Any,
) -> Any:
    return context.run(function, **kwargs)


def load_keyed_stage_worker(
    *,
    options: TransferOptions,
    worker_stage_state: WorkerStageState,
    read_retry_cnt: int,
    insert_retry_cnt: int,
) -> int:
    stage_state = worker_stage_state.stage_state
    connection_refs = TransferConnectionRefs(
        source={"connection": get_sql_connection(options.from_db_key)},
        target={},
    )
    total_rows = 0
    try:
        for transfer_slice in worker_stage_state.transfer_slices:
            worker_options = replace(options, source_sql=transfer_slice.source_sql)
            transfer_key_label = format_transfer_slice_log_label(
                options,
                transfer_slice,
            )
            worker_options = prepare_row_count_validated_options(
                options=worker_options,
                connection_refs=connection_refs,
                stage_state=stage_state,
                slice_index=transfer_slice.index,
                transfer_key_label=transfer_key_label,
            )
            streamed_rows = load_stage_batches(
                options=worker_options,
                connection_refs=connection_refs,
                stage_state=stage_state,
                read_retry_cnt=read_retry_cnt,
                insert_retry_cnt=insert_retry_cnt,
                slice_index=transfer_slice.index,
                transfer_key_label=transfer_key_label,
            )
            validate_slice_row_count(
                options=worker_options,
                stage_state=stage_state,
                slice_index=transfer_slice.index,
                transfer_key_label=transfer_key_label,
                streamed_rows=streamed_rows,
            )
            cleanup_sources(worker_options, connection_refs.source, stage_state)
            total_rows += streamed_rows
        return total_rows
    finally:
        cleanup_sources_and_close(options, connection_refs.source, stage_state)


def consolidate_keyed_worker_stages(
    *,
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    worker_stage_states: list[WorkerStageState],
    stage_state: TransferStageState,
) -> None:
    if options.write_mode == "upsert":
        return
    if len(worker_stage_states) <= 1:
        return
    aggregate_stage_table = stage_state.stage_table
    if aggregate_stage_table is None:
        raise RuntimeError("Expected aggregate stage table to be initialized.")
    for worker_stage_state in worker_stage_states[1:]:
        worker_stage_table = worker_stage_state.stage_state.stage_table
        if worker_stage_table is None:
            raise RuntimeError("Expected worker stage table to be initialized.")
        _run_with_fresh_target_connection(
            options,
            "consolidate_stage",
            lambda target_ref, worker_stage_table=worker_stage_table: insert_from_table(
                options.to_db_backend,
                target_ref["connection"],
                aggregate_stage_table,
                worker_stage_table,
                column_types=stage_state.stage_column_types,
                query_label=options.query_label,
            ),
        )


def load_stage_batches(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    read_retry_cnt: int,
    insert_retry_cnt: int,
    slice_index: int | None = None,
    transfer_key_label: str | None = None,
) -> int:
    if options.trino_mode == "parquet":
        return load_parquet_stage_batches(
            options=options,
            connection_refs=connection_refs,
            stage_state=stage_state,
            read_retry_cnt=read_retry_cnt,
            slice_index=slice_index,
            transfer_key_label=transfer_key_label,
        )

    total_rows = 0
    estimated_total_rows = None
    if options.progress and options.estimate_total_rows:
        estimated_total_rows = estimate_source_rows(
            options,
            connection_refs.source["connection"],
        )
    progress_bar = make_transfer_progress_bar(
        options,
        total=estimated_total_rows,
        base_tqdm=tqdm,
    )
    progress_tracker = ProgressTracker(progress_bar)
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
    insert_page_sizing = get_backend_adapter(
        options.to_db_backend,
    ).transfer_insert_page_sizing(gp_insert_chunk_size=options.gp_insert_chunk_size)
    gp_insert_chunk_sizer = (
        None
        if insert_page_sizing is None
        else AdaptiveBatchSizer(
            enabled=options.adaptive_batch_size,
            current_size=insert_page_sizing.initial_size,
            min_size=insert_page_sizing.min_size,
            max_size=insert_page_sizing.max_size,
            target_seconds=options.target_batch_seconds,
            optimize_by_rows_per_second=True,
            target_rows_per_second_window=options.target_rows_per_second_window,
            target_rows_per_second_deadband=options.target_rows_per_second_deadband,
            adaptive_batch_size_step=options.adaptive_batch_size_step,
        )
    )
    next_ordinal = 1
    try:
        for source_batch in iter_source_batches(
            options.from_db_key,
            options.from_db_backend,
            connection_refs.source,
            options.source_sql,
            options.batch_size,
            retry_cnt=read_retry_cnt,
            timeout_increment=options.timeout_increment,
            query_label=options.query_label,
            get_batch_size=lambda: batch_sizer.current_size,
            disable_ch_query_limit=disable_query_limit_for_transfer_reads(
                options.from_db_backend,
            ),
        ):
            if source_batch.empty:
                continue
            batch = get_backend_adapter(options.from_db_backend).normalize_transfer_source_batch(
                source_batch,
                stage_state.source_column_types,
            )
            if not stage_state.source_columns:
                stage_state.source_columns = list(batch.columns)
            if stage_state.internal_columns is None:
                stage_state.internal_columns = resolve_internal_columns(
                    batch.columns,
                    options.from_db_backend,
                    table_schema_names=(options.table_schema or {}).keys(),
                )
            batch = _append_transfer_identity_columns(
                batch,
                options=options,
                stage_state=stage_state,
                slice_id=0 if slice_index is None else slice_index,
                start_ordinal=next_ordinal,
            )
            next_ordinal += batch.row_count

            if stage_state.first_non_empty_batch is None:
                _run_with_fresh_target_connection(
                    options,
                    "create_stage",
                    lambda target_ref: initialize_stage_for_first_batch(
                        options=options,
                        connection_refs=TransferConnectionRefs(
                            source=connection_refs.source,
                            target=target_ref,
                        ),
                        stage_state=stage_state,
                        batch=batch,
                    ),
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

            inserted_rows = _run_with_fresh_target_connection(
                options,
                "insert_stage",
                lambda target_ref: insert_rows_batch(
                    options.to_db_backend,
                    target_ref,
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
                        update_gp_insert_chunk_sizer if gp_insert_chunk_sizer is not None else None
                    ),
                    connection_key=options.to_db_key,
                    rollback_fn=rollback_quietly,
                    replace_connection_fn=replace_connection,
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
                f"{rows_per_second:,.2f}" if rows_per_second is not None else "N/A"
            )
            time_print(
                f"Transferred batch of "
                f"{format_transfer_progress_count(inserted_rows)} row(s) "
                f"{format_transfer_key_log_fragment(transfer_key_label)}"
                f"to {stage_state.stage_table} in "
                f"{_format_duration(current_batch_duration_seconds)} "
                f"({rows_per_second_text} row/s); total transferred "
                f"{format_transfer_progress_count(total_rows)} row(s)",
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
    transfer_key_label: str | None = None,
) -> int:
    return _load_parquet_stage_batches_impl(
        options,
        connection_refs,
        stage_state,
        read_retry_cnt,
        slice_index,
        transfer_key_label,
        ensure_dependencies=ensure_parquet_staging_dependencies,
        write_batch=write_batch_to_parquet_stage,
        initialize_stage=_initialize_parquet_stage_for_first_batch,
        row_group_size_fn=parquet_row_group_size,
        estimate_rows=estimate_source_rows,
        progress_bar_factory=make_transfer_progress_bar,
        source_batches=iter_source_batches,
    )


def _initialize_parquet_stage_for_first_batch(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    batch: RowBatch,
) -> None:
    _initialize_parquet_stage_for_first_batch_impl(
        options,
        connection_refs,
        stage_state,
        batch,
        create_stage=create_parquet_stage_table,
    )
