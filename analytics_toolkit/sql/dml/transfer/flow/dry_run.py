from __future__ import annotations

from typing import Any

import pandas as pd
from sqlglot import exp, parse_one

from ....backends import get_backend_adapter
from ....core.identifiers import sqlglot_dialect
from ....ddl.api import _build_create_table_sqls
from ....execution.plan_steps import (
    add_create_table_placeholder_step,
    add_create_table_steps,
    add_insert_from_stage_step,
)
from ....execution.plans import SqlPlan
from ...load.stage import build_stage_table_name
from ...table.write_modes import (
    build_upsert_stage_placeholder_sqls,
    build_upsert_stage_sqls,
)
from ..runtime.models import TransferOptions
from .parquet_stage import build_stage_external_location


def dry_run_transfer_options(
    options: TransferOptions,
    stage_tables: list[str],
    insert_page_sizing: Any | None,
    *,
    gp_partitions: object,
) -> dict[str, object]:
    slices = options.transfer_slices
    staged = slices is not None and options.source_transfer_staging_schema is not None
    return {
        "write_mode": options.write_mode,
        "transfer_id": options.transfer_id,
        "canonical_destination_identity": options.canonical_destination_identity,
        "destination_hash": options.destination_hash,
        "batch_size": options.batch_size,
        "adaptive_batch_size": options.adaptive_batch_size,
        "min_batch_size": options.min_batch_size,
        "max_batch_size": options.max_batch_size,
        "adaptive_batch_size_step": options.adaptive_batch_size_step,
        "target_batch_seconds": options.target_batch_seconds,
        "min_batch_seconds": options.min_batch_seconds,
        "max_batch_seconds": options.max_batch_seconds,
        "target_batch_memory_mb": options.target_batch_memory_mb,
        "min_batch_memory_mb": options.min_batch_memory_mb,
        "max_batch_memory_mb": options.max_batch_memory_mb,
        "target_rows_per_second_window": options.target_rows_per_second_window,
        "target_rows_per_second_deadband": options.target_rows_per_second_deadband,
        "key_columns": options.key_columns,
        "upsert_partition_column": options.upsert_partition_column,
        "gp_distributed_by_key": options.gp_distributed_by_key,
        "gp_partitions": gp_partitions,
        "gp_insert_chunk_size": options.gp_insert_chunk_size,
        "adaptive_gp_insert_chunk_size": bool(
            insert_page_sizing is not None and options.adaptive_batch_size
        ),
        "initial_gp_insert_chunk_size": (
            insert_page_sizing.initial_size if insert_page_sizing else None
        ),
        "trino_insert_chunk_size": options.trino_insert_chunk_size,
        "transfer_staging_location": options.transfer_staging_location,
        "trino_mode": options.trino_mode,
        "from_table": options.source_table,
        "source_table": options.source_table,
        "transfer_keys": options.transfer_keys,
        "transfer_key_expressions": options.transfer_key_expressions,
        "transfer_key_values": options.transfer_key_values,
        "concurrency": options.transfer_concurrency.legacy_value,
        "read_concurrency": (
            options.transfer_concurrency.requested_read
            if options.transfer_concurrency.split_requested
            else None
        ),
        "write_concurrency": (
            options.transfer_concurrency.requested_write
            if options.transfer_concurrency.split_requested
            else None
        ),
        "ignore_source_staging": options.ignore_source_staging,
        "source_staging_mode": "source_staged" if staged else "direct",
        "effective_read_concurrency": options.transfer_concurrency.effective_read,
        "effective_write_concurrency": options.transfer_concurrency.effective_write,
        "queue_capacity": (
            options.transfer_concurrency.effective_write if slices and not staged else None
        ),
        "reader_slice_assignments": dry_run_reader_slice_assignments(options),
        "source_stage_count": options.transfer_concurrency.effective_read if staged else 0,
        "source_stage_phase_barrier": staged,
        "writer_scheduling": "whole_key" if staged else "batch_queue",
        "target_stage_count": len(stage_tables),
        "transfer_slice_count": len(slices) if slices is not None else None,
        "worker_stage_count": len(stage_tables),
        "stage_tables": stage_tables,
        "aggregate_stage_table": stage_tables[0],
        "table_schema": options.table_schema,
        "partition_by": options.partition_by,
        "order_by": options.order_by,
        "ch_engine": options.ch_engine,
        "ch_cluster": options.ch_cluster,
        "ch_sharding_key": options.ch_sharding_key,
        "ch_only_shard": options.ch_only_shard,
        "estimate_total_rows": options.estimate_total_rows,
        "validate_row_count": options.validate_row_count,
        "ch_count_limit_read": options.ch_count_limit_read,
        "runtime_collision_allocation": (
            "preferred stage names are shown; runtime collisions allocate a different retained name"
        ),
        "internal_columns": (
            "<resolved after source schema inspection; generated names avoid collisions>"
        ),
    }


def dry_run_stage_table_names(options: TransferOptions) -> list[str]:
    worker_count = dry_run_worker_stage_count(options)
    if worker_count == 1:
        return [dry_run_stage_table_name(options)]
    return [
        dry_run_stage_table_name(options, worker_index=worker_index)
        for worker_index in range(worker_count)
    ]


def dry_run_worker_stage_count(options: TransferOptions) -> int:
    if (
        options.transfer_slices is None
        and options.source_transfer_staging_schema is not None
        and options.trino_mode != "parquet"
    ):
        return options.transfer_concurrency.effective_write
    if (
        options.transfer_slices is None
        or options.trino_mode == "parquet"
        or options.transfer_concurrency.effective_write <= 1
    ):
        return 1
    return min(options.transfer_concurrency.effective_write, len(options.transfer_slices))


def dry_run_stage_table_name(
    options: TransferOptions,
    *,
    worker_index: int | None = None,
) -> str:
    runtime_id = options.transfer_id or "<runtime-transfer-id>"
    suffix = (
        f"{runtime_id}__w00000" if worker_index is None else f"{runtime_id}__w{worker_index:05d}"
    )
    try:
        return build_stage_table_name(
            options.to_db_backend,
            options.target_table,
            transfer_staging_schema=(
                options.transfer_parquet_staging_schema or options.transfer_staging_schema
            ),
            transfer_staging_username=options.transfer_staging_username,
            random_suffix=suffix,
            destination_hash=options.destination_hash,
        )
    except Exception:
        return f"{options.target_table}__stage__{suffix}"


def dry_run_final_upsert_stage_table_name(options: TransferOptions) -> str:
    return dry_run_stage_table_name(options).replace("__w00000", "__upsert")


def source_batches_label(
    options: TransferOptions,
    worker_index: int | None = None,
) -> str:
    if options.transfer_slices is None:
        return "source batches"
    if worker_index is None or dry_run_worker_stage_count(options) == 1:
        return "shared keyed source slice batches"
    worker_count = dry_run_worker_stage_count(options)
    slice_indexes = [
        transfer_slice.index
        for transfer_slice in options.transfer_slices[worker_index::worker_count]
    ]
    return f"worker {worker_index} streamed keyed source slice batches {slice_indexes}"


def dry_run_reader_slice_assignments(options: TransferOptions) -> dict[int, list[int]] | None:
    if options.transfer_slices is None:
        return None
    worker_count = options.transfer_concurrency.effective_read
    return {
        worker_index: [item.index for item in options.transfer_slices[worker_index::worker_count]]
        for worker_index in range(worker_count)
    }


def dry_run_stage_external_location(options: TransferOptions) -> str | None:
    if not options.transfer_staging_location:
        return None
    try:
        return build_stage_external_location(
            options,
            stage_suffix=options.transfer_id or "<runtime-transfer-id>",
        )
    except Exception:
        return options.transfer_staging_location.rstrip("/") + "/__stage__dryrun/"


def resolve_dry_run_upsert_columns(options: TransferOptions) -> list[str] | None:
    if options.table_schema is not None:
        return list(options.table_schema)
    return infer_source_select_columns(
        options.source_sql,
        source_backend=options.from_db_backend,
    )


def add_upsert_target_dry_run_steps(
    plan: SqlPlan,
    options: TransferOptions,
    *,
    stage_table: str,
    stage_tables: list[str],
) -> None:
    target_adapter = get_backend_adapter(options.to_db_backend)
    uses_partition_replacement_upsert = target_adapter.uses_partition_replacement_upsert()
    columns = resolve_dry_run_upsert_columns(options)
    final_stage_table = dry_run_final_upsert_stage_table_name(options)
    if uses_partition_replacement_upsert:
        _add_final_upsert_stage_create_step(plan, options, final_stage_table)
    upsert_sqls = (
        build_upsert_stage_sqls(
            options.to_db_backend,
            options.target_table,
            stage_table,
            columns=columns,
            key_columns=options.key_columns or [],
            column_types=options.table_schema,
            ch_cluster=options.ch_cluster,
            ch_only_shard=options.ch_only_shard,
            query_label=options.query_label,
            upsert_partition_column=options.upsert_partition_column,
            final_stage_table=(final_stage_table if uses_partition_replacement_upsert else None),
            incoming_stage_tables=stage_tables,
            trino_partition_drop_sql_template=(options.trino_upsert_partition_drop_sql_template),
        )
        if columns is not None
        else build_upsert_stage_placeholder_sqls(
            options.to_db_backend,
            options.target_table,
            stage_table,
            key_columns=options.key_columns or [],
            ch_cluster=options.ch_cluster,
            ch_only_shard=options.ch_only_shard,
            query_label=options.query_label,
            upsert_partition_column=options.upsert_partition_column,
            final_stage_table=(final_stage_table if uses_partition_replacement_upsert else None),
            incoming_stage_tables=stage_tables,
            trino_partition_drop_sql_template=(options.trino_upsert_partition_drop_sql_template),
        )
    )
    plan.extend(
        upsert_sqls,
        alias=options.to_db_key,
        backend=options.to_db_backend,
        phase="upsert_target",
        target_table=options.target_table,
    )


def add_insert_target_dry_run_steps(
    plan: SqlPlan,
    options: TransferOptions,
    *,
    stage_table: str,
) -> None:
    target_adapter = get_backend_adapter(options.to_db_backend)
    if options.table_schema is None:
        add_create_table_placeholder_step(
            plan,
            alias=options.to_db_key,
            backend=options.to_db_backend,
            table_name=options.target_table,
            query_label=options.query_label,
        )
    else:
        add_create_table_steps(
            plan,
            _build_create_table_sqls(
                options.to_db_backend,
                options.target_table,
                pd.DataFrame(columns=list(options.table_schema)),
                table_schema=options.table_schema,
                gp_distributed_by_key=options.gp_distributed_by_key,
                gp_partitions=options.gp_partitions,
                partition_by=options.partition_by,
                order_by=options.order_by,
                ch_engine=options.ch_engine,
                ch_cluster=options.ch_cluster,
                ch_sharding_key=options.ch_sharding_key,
                ch_distributed_table=(
                    target_adapter.supports_distributed_table_targets()
                    and not options.ch_only_shard
                ),
                ch_only_shard=options.ch_only_shard,
                ch_replace_table=(
                    target_adapter.supports_distributed_table_targets()
                    and options.write_mode == "replace"
                    and not options.ch_only_shard
                ),
                query_label=options.query_label,
                ddl_properties=options.regular_ddl_properties,
                ch_creation_policy=options.regular_ch_policy,
            ),
            alias=options.to_db_key,
            backend=options.to_db_backend,
            table_name=options.target_table,
        )
    add_insert_from_stage_step(
        plan,
        alias=options.to_db_key,
        backend=options.to_db_backend,
        target_table=options.target_table,
        stage_table=stage_table,
        phase="insert_target",
        query_label=options.query_label,
    )


def _add_final_upsert_stage_create_step(
    plan: SqlPlan,
    options: TransferOptions,
    final_stage_table: str,
) -> None:
    if options.table_schema is None:
        add_create_table_placeholder_step(
            plan,
            alias=options.to_db_key,
            backend=options.to_db_backend,
            phase="create_final_upsert_stage",
            table_name=final_stage_table,
            query_label=options.query_label,
        )
        return
    add_create_table_steps(
        plan,
        _build_create_table_sqls(
            options.to_db_backend,
            final_stage_table,
            pd.DataFrame(columns=list(options.table_schema)),
            table_schema=options.table_schema,
            gp_distributed_by_key=options.gp_distributed_by_key,
            query_label=options.query_label,
        ),
        alias=options.to_db_key,
        backend=options.to_db_backend,
        phase="create_final_upsert_stage",
        table_name=final_stage_table,
    )


def infer_source_select_columns(
    source_sql: str,
    *,
    source_backend: str,
) -> list[str] | None:
    try:
        expression = parse_one(
            source_sql.strip().rstrip(";"),
            read=sqlglot_dialect(source_backend),
        )
    except Exception:
        return None

    if not isinstance(expression, exp.Select):
        return None

    columns: list[str] = []
    for projection in expression.expressions:
        if isinstance(projection, exp.Star) or projection.find(exp.Star) is not None:
            return None
        column_name = projection.alias_or_name
        if not column_name or column_name == "*":
            return None
        columns.append(str(column_name))
    return columns or None
