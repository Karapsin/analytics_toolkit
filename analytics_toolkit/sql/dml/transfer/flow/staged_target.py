from __future__ import annotations

# ruff: noqa: EM101, PLR0913, TC001, TID252, TRY003
from typing import Any

import pandas as pd

from ....dml.load.load_sql_table import insert_rows_batch
from ....dml.load.stage import cleanup_stage_table, create_stage_table
from ..runtime.models import RowBatch, TransferConnectionRefs, TransferOptions, TransferStageState
from ..runtime.retry import replace_connection, rollback_quietly, run_with_retry
from .parquet_stage import (
    create_parquet_stage_table,
    ensure_parquet_staging_dependencies,
    parquet_row_group_size,
    write_batch_to_parquet_stage,
)


def prepare_shared_parquet_stage(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    stage_state: TransferStageState,
) -> bool:
    if options.trino_mode != "parquet":
        return False
    ensure_parquet_staging_dependencies()
    create_parquet_stage_table(options, connection_refs, stage_state)
    if stage_state.stage_table is None:
        raise RuntimeError("Parquet stage creation did not return a stage table.")
    stage_state.stage_tables = [stage_state.stage_table]
    stage_state.first_non_empty_batch = pd.DataFrame(columns=stage_state.source_columns)
    return True


def create_sql_worker_stages(
    options: TransferOptions,
    target_ref: dict[str, Any],
    stage_state: TransferStageState,
    *,
    worker_count: int,
    create_fn: Any = create_stage_table,
    cleanup_fn: Any = cleanup_stage_table,
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
        stage_table = create_fn(
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
        cleanup_fn(
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


def select_unkeyed_worker_stages(
    options: TransferOptions,
    target_ref: dict[str, Any],
    stage_state: TransferStageState,
    *,
    worker_count: int,
    parquet_target: bool,
    sql_stage_fn: Any = create_sql_worker_stages,
    replace_target_fn: Any = replace_connection,
) -> tuple[list[str], list[str]]:
    if parquet_target:
        if stage_state.stage_table is None:
            raise RuntimeError("Expected a shared Parquet target stage.")
        return [stage_state.stage_table] * worker_count, [stage_state.stage_table]
    replace_target_fn(options.to_db_key, target_ref)
    worker_tables = sql_stage_fn(
        options,
        target_ref,
        stage_state,
        worker_count=worker_count,
    )
    return worker_tables, list(worker_tables)


def write_source_staged_batch(
    options: TransferOptions,
    target_ref: dict[str, Any],
    stage_state: TransferStageState,
    stage_table: str,
    batch: RowBatch,
    *,
    worker_id: int,
    slice_index: int,
    file_index: int,
    start_ordinal: int,
    stop_ordinal: int,
    insert_retry_cnt: int,
    retry_fn: Any = run_with_retry,
    insert_fn: Any = insert_rows_batch,
    safe_exception_logging: bool = False,
    log_prefix: str = "",
) -> int:
    if getattr(options, "trino_mode", None) == "parquet":
        if stage_state.stage_external_location is None:
            raise RuntimeError("Expected Parquet stage external location.")
        pa, pq, fsspec_module = ensure_parquet_staging_dependencies()
        return write_batch_to_parquet_stage(
            batch,
            file_index=file_index,
            slice_index=slice_index,
            stage_external_location=stage_state.stage_external_location,
            pa=pa,
            pq=pq,
            fsspec_module=fsspec_module,
            row_group_size=parquet_row_group_size(options),
            transfer_id=options.transfer_id,
            worker_id=worker_id,
            start_ordinal=start_ordinal,
            stop_ordinal=stop_ordinal,
            storage_options=options.parquet_storage_options,
        )
    result = insert_fn(
        options.to_db_backend,
        target_ref,
        stage_table,
        batch.columns,
        batch.rows,
        retry_fn=retry_fn,
        retry_cnt=insert_retry_cnt,
        timeout_increment=options.timeout_increment,
        target_column_types=stage_state.stage_column_types,
        gp_insert_chunk_size=getattr(options, "gp_insert_chunk_size", None),
        trino_insert_chunk_size=getattr(options, "trino_insert_chunk_size", None),
        query_label=options.query_label,
        connection_key=options.to_db_key,
        rollback_fn=rollback_quietly,
        replace_connection_fn=replace_connection,
        safe_exception_logging=safe_exception_logging,
        log_prefix=log_prefix,
    )
    return batch.row_count if result is None else int(result)


__all__ = [
    "create_sql_worker_stages",
    "prepare_shared_parquet_stage",
    "select_unkeyed_worker_stages",
    "write_source_staged_batch",
]
