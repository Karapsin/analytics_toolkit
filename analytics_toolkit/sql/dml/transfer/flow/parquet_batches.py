from __future__ import annotations

# ruff: noqa: B009, B023, EM101, I001, PLR0913, TC003, TID252, TRY003

from collections.abc import Callable

import pandas as pd
from tqdm import tqdm

from analytics_toolkit.general import time_print

from ....backends import get_backend_adapter
from ....ddl.schema import validate_table_schema_columns
from ....connection.get_sql_connection import get_sql_connection
from ...table.table_validation import (
    validate_key_columns_in_columns,
    validate_upsert_partition_column_in_columns,
)
from ..io.source import iter_source_batches
from ..runtime.models import RowBatch, TransferConnectionRefs, TransferOptions, TransferStageState
from ..runtime.retry import run_with_fresh_connection
from .estimate import estimate_source_rows
from .logging import ProgressTracker, format_transfer_key_log_fragment
from .parquet_stage import (
    create_parquet_stage_table,
    ensure_parquet_staging_dependencies,
    parquet_row_group_size,
    write_batch_to_parquet_stage,
)
from .progress import format_transfer_progress_count, make_transfer_progress_bar
from .row_counts import disable_query_limit_for_transfer_reads
from .stage import _with_internal_column_types
from .stage_identity import resolve_internal_columns


def load_parquet_stage_batches(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    read_retry_cnt: int,
    slice_index: int | None = None,
    transfer_key_label: str | None = None,
    ensure_dependencies: object = ensure_parquet_staging_dependencies,
    write_batch: object = write_batch_to_parquet_stage,
    initialize_stage: object = None,
    row_group_size_fn: object = parquet_row_group_size,
    estimate_rows: object = estimate_source_rows,
    progress_bar_factory: object = make_transfer_progress_bar,
    source_batches: object = iter_source_batches,
) -> int:
    pa, pq, fsspec_module = ensure_dependencies()  # type: ignore[operator]
    initialize_stage = initialize_stage or initialize_parquet_stage_for_first_batch
    total_rows = 0
    file_index = 0
    row_group_size = row_group_size_fn(options)  # type: ignore[operator]
    estimated_total_rows = None
    if options.progress and options.estimate_total_rows:
        estimated_total_rows = estimate_rows(  # type: ignore[operator]
            options,
            connection_refs.source["connection"],
        )
    progress_bar = progress_bar_factory(  # type: ignore[operator]
        options,
        total=estimated_total_rows,
        base_tqdm=tqdm,
    )
    progress_tracker = ProgressTracker(progress_bar)
    next_ordinal = 1
    try:
        for source_batch in source_batches(  # type: ignore[operator]
            options.from_db_key,
            options.from_db_backend,
            connection_refs.source,
            options.source_sql,
            row_group_size,
            retry_cnt=read_retry_cnt,
            timeout_increment=options.timeout_increment,
            query_label=options.query_label,
            get_batch_size=lambda: row_group_size,
            disable_ch_query_limit=disable_query_limit_for_transfer_reads(
                options.from_db_backend,
            ),
        ):
            if source_batch.empty:
                continue
            batch = get_backend_adapter(options.from_db_backend).normalize_transfer_source_batch(
                source_batch,
                stage_state.source_column_types or {},
            )
            if not stage_state.source_columns:
                stage_state.source_columns = list(batch.columns)
            if stage_state.internal_columns is None:
                stage_state.internal_columns = resolve_internal_columns(
                    batch.columns,
                    options.from_db_backend,
                    table_schema_names=(options.table_schema or {}).keys(),
                )
            batch = append_transfer_identity_columns(
                batch,
                options=options,
                stage_state=stage_state,
                slice_id=0 if slice_index is None else slice_index,
                start_ordinal=next_ordinal,
            )
            next_ordinal += batch.row_count
            if stage_state.first_non_empty_batch is None:
                run_with_fresh_connection(
                    options.to_db_key,
                    "create_stage",
                    lambda target_ref: initialize_stage(  # type: ignore[operator]
                        options,
                        TransferConnectionRefs(
                            source=connection_refs.source,
                            target=target_ref,
                        ),
                        stage_state,
                        batch,
                    ),
                    open_connection=get_sql_connection,
                )
            if stage_state.stage_external_location is None:
                raise RuntimeError("Expected Parquet stage location to be initialized.")
            progress_tracker.start_batch()
            inserted_rows = write_batch(  # type: ignore[operator]
                batch,
                file_index=file_index,
                slice_index=slice_index,
                stage_external_location=stage_state.stage_external_location,
                pa=pa,
                pq=pq,
                fsspec_module=fsspec_module,
                row_group_size=row_group_size,
                transfer_id=options.transfer_id,
            )
            file_index += 1
            progress_tracker.update(inserted_rows)
            progress_tracker.complete_batch(inserted_rows)
            total_rows += inserted_rows
            time_print(
                f"Wrote Parquet transfer batch of "
                f"{format_transfer_progress_count(inserted_rows)} row(s) "
                f"{format_transfer_key_log_fragment(transfer_key_label)}"
                f"to {stage_state.stage_external_location}; total transferred "
                f"{format_transfer_progress_count(total_rows)} row(s)",
                connection=options.to_db_key,
                backend=options.to_db_backend,
            )
        return total_rows
    finally:
        progress_bar.close()


def append_transfer_identity_columns(
    batch: RowBatch,
    *,
    options: TransferOptions,
    stage_state: TransferStageState,
    slice_id: int,
    start_ordinal: int,
) -> RowBatch:
    if options.transfer_id is None:
        return batch
    internal = stage_state.internal_columns
    if internal is None or options.canonical_destination_identity is None:
        raise RuntimeError("Transfer runtime identity was not initialized.")
    return RowBatch(
        columns=[*batch.columns, *internal.names()],
        rows=[
            (
                *row,
                options.transfer_id,
                options.canonical_destination_identity,
                slice_id,
                start_ordinal + offset,
            )
            for offset, row in enumerate(batch.rows)
        ],
    )


def initialize_parquet_stage_for_first_batch(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
    batch: RowBatch,
    create_stage: object = create_parquet_stage_table,
) -> None:
    source_columns = stage_state.source_columns or list(batch.columns)
    stage_state.source_columns = list(source_columns)
    if options.table_schema is not None:
        source_types = validate_table_schema_columns(options.table_schema, source_columns)
    elif stage_state.stage_column_types is None:
        source_batch = RowBatch(
            columns=source_columns,
            rows=[row[: len(source_columns)] for row in batch.rows],
        )
        infer_types: Callable[[RowBatch], dict[str, str]] = getattr(
            get_backend_adapter(options.to_db_backend),
            "infer_parquet_stage_column_types_from_rows",
        )
        source_types = infer_types(source_batch)
    else:
        source_types = stage_state.stage_column_types
    stage_state.stage_column_types = _with_internal_column_types(
        source_types,
        options,
        stage_state,
    )
    validate_key_columns_in_columns(options.key_columns, source_columns)
    validate_upsert_partition_column_in_columns(
        options.upsert_partition_column,
        source_columns,
    )
    stage_state.first_non_empty_batch = pd.DataFrame.from_records(
        [row[: len(source_columns)] for row in batch.rows[:1]],
        columns=source_columns,
    )
    create_stage(  # type: ignore[operator]
        options=options,
        connection_refs=connection_refs,
        stage_state=stage_state,
    )
