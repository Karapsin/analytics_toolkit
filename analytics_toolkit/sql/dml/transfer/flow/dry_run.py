from __future__ import annotations

# ruff: noqa: BLE001, S608, TC001, TID252
from typing import Any

import pandas as pd
from sqlglot import exp, parse_one

from analytics_toolkit.sql.ddl.api import _gp_partition_plan_option

from ....backends import get_backend_adapter
from ....core.identifiers import sqlglot_dialect
from ....ddl.api import _build_create_table_sqls
from ....execution.plan_steps import (
    add_analyze_step,
    add_cleanup_stage_step,
    add_clear_target_steps,
    add_count_step,
    add_create_table_placeholder_step,
    add_create_table_steps,
    add_insert_from_stage_step,
    add_load_stage_step,
)
from ....execution.plans import SqlOperationMetadata, SqlPlan
from ...load.stage import build_stage_table_name
from ...table.write_modes import (
    build_upsert_stage_placeholder_sqls,
    build_upsert_stage_sqls,
)
from ..runtime.models import TransferOptions
from .parquet_stage import build_create_parquet_stage_table_sql, build_stage_external_location


def dry_run_transfer_options(
    options: TransferOptions,
    stage_tables: list[str],
    insert_page_sizing: Any | None,
    *,
    gp_partitions: object,
) -> dict[str, object]:
    slices = options.transfer_slices
    source_staged = options.source_transfer_staging_schema is not None
    lazy_keyed_staging = slices is not None and source_staged
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
        "s3_transfer_staging_location": options.s3_transfer_staging_location,
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
        "requested_read_concurrency": options.transfer_concurrency.requested_read,
        "requested_write_concurrency": options.transfer_concurrency.requested_write,
        "soft_concurrency_cap": options.transfer_concurrency.soft_concurrency_cap,
        "hard_concurrency_cap": options.transfer_concurrency.hard_concurrency_cap,
        "soft_limited_read_concurrency": options.transfer_concurrency.soft_limited_read,
        "soft_limited_write_concurrency": options.transfer_concurrency.soft_limited_write,
        "ignore_source_staging": options.ignore_source_staging,
        "source_staging_mode": "source_staged" if source_staged else "direct",
        "effective_read_concurrency": options.transfer_concurrency.effective_read,
        "effective_write_concurrency": options.transfer_concurrency.effective_write,
        "source_connection_limit": options.transfer_concurrency.effective_read,
        "target_connection_limit": options.transfer_concurrency.effective_write,
        "queue_capacity": (options.transfer_concurrency.effective_write if slices else None),
        "batch_queue_capacity_per_writer": 1 if lazy_keyed_staging else None,
        "resident_batch_slots": (
            options.transfer_concurrency.effective_write * 2 if lazy_keyed_staging else 1
        ),
        "target_batch_memory_scope": (
            "aggregate_resident_batches"
            if lazy_keyed_staging and options.target_batch_memory_bytes is not None
            else "per_batch"
        ),
        "target_memory_bytes_per_resident_batch": (
            max(
                1,
                options.target_batch_memory_bytes
                // (options.transfer_concurrency.effective_write * 2),
            )
            if lazy_keyed_staging and options.target_batch_memory_bytes is not None
            else options.target_batch_memory_bytes
        ),
        "reader_scheduling": (
            "dynamic_pending_key_claims"
            if lazy_keyed_staging
            else ("static_round_robin" if slices is not None else None)
        ),
        "reader_slice_assignments": dry_run_reader_slice_assignments(options),
        "source_stage_count": (
            len(slices)
            if lazy_keyed_staging and slices is not None
            else int(source_staged)
        ),
        "live_source_stage_limit": (
            options.transfer_concurrency.effective_read
            + options.transfer_concurrency.effective_write
            if lazy_keyed_staging
            else None
        ),
        "source_stage_phase_barrier": (
            False if lazy_keyed_staging else (True if source_staged else None)
        ),
        "source_stage_creation": (
            "lazy_per_key"
            if lazy_keyed_staging
            else ("single_snapshot" if source_staged else None)
        ),
        "source_stage_lifecycle": (
            "per_key_ctas_count_stream_validate_acknowledge_drop"
            if lazy_keyed_staging
            else ("snapshot_then_stream_and_drop" if source_staged else None)
        ),
        "writer_scheduling": "whole_key" if lazy_keyed_staging else "batch_queue",
        "target_stage_count": len(stage_tables),
        "target_stage_maximum": len(stage_tables) if lazy_keyed_staging else None,
        "target_stage_count_is_maximum": lazy_keyed_staging,
        "target_stage_creation": (
            "lazy_first_non_empty_key" if lazy_keyed_staging else "eager"
        ),
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
        "ch_ddl_ready_timeout_extension_cnt": options.ch_ddl_ready_timeout_extension_cnt,
        "ch_ddl_wait_policy": (
            options.regular_ch_policy.ddl_wait_policy
            if options.regular_ch_policy is not None
            else None
        ),
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


def build_transfer_table_plan(options: TransferOptions) -> SqlPlan:
    target_adapter = get_backend_adapter(options.to_db_backend)
    stage_tables = dry_run_stage_table_names(options)
    stage_table = stage_tables[0]
    insert_page_sizing = target_adapter.transfer_insert_page_sizing(
        gp_insert_chunk_size=options.gp_insert_chunk_size
    )
    stage_external_location = (
        dry_run_stage_external_location(options) if options.trino_mode == "parquet" else None
    )
    plan = _new_transfer_plan(
        options,
        stage_tables,
        stage_external_location,
        insert_page_sizing,
    )
    lazy_keyed_staging = _uses_lazy_keyed_source_staging(options)
    if lazy_keyed_staging:
        _add_target_stage_templates(
            plan,
            options,
            stage_tables,
            stage_external_location,
            lazy=True,
        )
        _add_lazy_key_lifecycle_steps(plan, options)
    else:
        _add_static_source_steps(plan, options)
        _add_target_stage_templates(
            plan,
            options,
            stage_tables,
            stage_external_location,
            lazy=False,
        )
        _add_static_load_steps(plan, options, stage_tables, stage_external_location)
    _add_consolidation_steps(plan, options, stage_tables, lazy=lazy_keyed_staging)
    _add_destination_steps(plan, options, stage_table, stage_tables)
    _add_target_cleanup_steps(
        plan,
        options,
        stage_tables,
        stage_external_location,
        lazy=lazy_keyed_staging,
    )
    return plan


def _new_transfer_plan(
    options: TransferOptions,
    stage_tables: list[str],
    stage_external_location: str | None,
    insert_page_sizing: Any | None,
) -> SqlPlan:
    stage_table = stage_tables[0]
    staged = _uses_lazy_keyed_source_staging(options)
    return SqlPlan(
        operation="transfer_table",
        source_alias=options.from_db_key,
        target_alias=options.to_db_key,
        source_backend=options.from_db_backend,
        target_backend=options.to_db_backend,
        target_table=options.target_table,
        options=dry_run_transfer_options(
            options,
            stage_tables,
            insert_page_sizing,
            gp_partitions=_gp_partition_plan_option(options.gp_partitions),
        ),
        metadata=SqlOperationMetadata(
            transfer_id=options.transfer_id,
            stage_table=stage_table,
            stage_external_location=stage_external_location,
            worker_stage_count=len(stage_tables),
            stage_tables=stage_tables,
            aggregate_stage_table=stage_table,
            requested_read_concurrency=options.transfer_concurrency.requested_read,
            requested_write_concurrency=options.transfer_concurrency.requested_write,
            soft_limited_read_concurrency=options.transfer_concurrency.soft_limited_read,
            soft_limited_write_concurrency=options.transfer_concurrency.soft_limited_write,
            soft_concurrency_cap=options.transfer_concurrency.soft_concurrency_cap,
            hard_concurrency_cap=options.transfer_concurrency.hard_concurrency_cap,
            effective_read_concurrency=options.transfer_concurrency.effective_read,
            effective_write_concurrency=options.transfer_concurrency.effective_write,
            ignore_source_staging=options.ignore_source_staging,
            source_staging_mode=(
                "source_staged" if options.source_transfer_staging_schema else "direct"
            ),
            source_stage_count=(
                len(options.transfer_slices or [])
                if staged
                else int(options.source_transfer_staging_schema is not None)
            ),
            live_source_stage_limit=(
                options.transfer_concurrency.effective_read
                + options.transfer_concurrency.effective_write
                if staged
                else None
            ),
        ),
    )


def _add_target_stage_templates(
    plan: SqlPlan,
    options: TransferOptions,
    stage_tables: list[str],
    stage_external_location: str | None,
    *,
    lazy: bool,
) -> None:
    phase = "create_stage_if_needed" if lazy else "create_stage"
    if options.trino_mode == "parquet":
        plan.add(
            build_create_parquet_stage_table_sql(
                stage_tables[0],
                options.table_schema,
                stage_external_location or "<stage external location>",
                query_label=options.query_label,
                ddl_properties={
                    **(options.staging_ddl_properties or {}),
                    **(options.parquet_ddl_properties or {}),
                },
            ),
            alias=options.to_db_key,
            backend=options.to_db_backend,
            phase=phase,
            target_table=stage_tables[0],
        )
        return
    for worker_stage_table in stage_tables:
        _add_target_stage_template(plan, options, worker_stage_table, phase=phase)


def _add_target_stage_template(
    plan: SqlPlan,
    options: TransferOptions,
    worker_stage_table: str,
    *,
    phase: str,
) -> None:
    if options.table_schema is None:
        add_create_table_placeholder_step(
            plan,
            alias=options.to_db_key,
            backend=options.to_db_backend,
            phase=phase,
            table_name=worker_stage_table,
            query_label=options.query_label,
        )
        return
    add_create_table_steps(
        plan,
        _build_create_table_sqls(
            options.to_db_backend,
            worker_stage_table,
            pd.DataFrame(columns=list(options.table_schema)),
            table_schema=options.table_schema,
            gp_distributed_by_key=options.gp_distributed_by_key,
            query_label=options.query_label,
            ddl_properties=options.staging_ddl_properties,
            ch_creation_policy=options.staging_ch_policy,
        ),
        alias=options.to_db_key,
        backend=options.to_db_backend,
        phase=phase,
        table_name=worker_stage_table,
    )


def _add_lazy_key_lifecycle_steps(plan: SqlPlan, options: TransferOptions) -> None:
    slices = options.transfer_slices or []
    for position, transfer_slice in enumerate(slices, start=1):
        source_stage = f"<lazy source stage slice {position}/{len(slices)}>"
        target_stage = "<dynamically assigned lazy target writer stage>"
        plan.add(
            f"CREATE TABLE {source_stage} AS\n{transfer_slice.source_sql}",
            alias=options.from_db_key,
            backend=options.from_db_backend,
            phase="materialize_source_stage",
            target_table=source_stage,
            query_label=options.query_label,
        )
        plan.add(
            f"SELECT COUNT(*) FROM {source_stage}",
            alias=options.from_db_key,
            backend=options.from_db_backend,
            phase="count_source_stage",
            target_table=source_stage,
            query_label=options.query_label,
        )
        add_load_stage_step(
            plan,
            alias=options.to_db_key,
            backend=options.to_db_backend,
            stage_table=target_stage,
            sql=(f"INSERT INTO {target_stage} SELECT * FROM <{source_batches_label(options)}>"),
            query_label=options.query_label,
        )
        plan.add(
            f"VALIDATE SLICE {position}/{len(slices)} IN {target_stage}",
            alias=options.to_db_key,
            backend=options.to_db_backend,
            phase="validate_key_stage",
            target_table=target_stage,
            query_label=options.query_label,
        )
        plan.add(
            f"DROP TABLE {source_stage}",
            alias=options.from_db_key,
            backend=options.from_db_backend,
            phase="drop_source_stage",
            target_table=source_stage,
            query_label=options.query_label,
        )


def _add_static_source_steps(plan: SqlPlan, options: TransferOptions) -> None:
    if options.transfer_slices is None:
        source_sqls = [options.source_sql]
    else:
        source_sqls = [item.source_sql for item in options.transfer_slices]
    for source_sql in source_sqls:
        plan.add(
            source_sql,
            alias=options.from_db_key,
            backend=options.from_db_backend,
            phase="read_source",
            query_label=options.query_label,
        )


def _add_static_load_steps(
    plan: SqlPlan,
    options: TransferOptions,
    stage_tables: list[str],
    stage_external_location: str | None,
) -> None:
    if options.trino_mode == "parquet":
        add_load_stage_step(
            plan,
            alias=options.to_db_key,
            backend=options.to_db_backend,
            stage_table=stage_tables[0],
            sql=(
                "WRITE PARQUET FILES TO "
                f"{stage_external_location or '<stage external location>'} "
                f"FROM <{source_batches_label(options)}>"
            ),
            query_label=options.query_label,
        )
        return
    for worker_index, worker_stage_table in enumerate(stage_tables):
        add_load_stage_step(
            plan,
            alias=options.to_db_key,
            backend=options.to_db_backend,
            stage_table=worker_stage_table,
            sql=(
                f"INSERT INTO {worker_stage_table} SELECT * "
                f"FROM (<{source_batches_label(options, worker_index)}>)"
            ),
            query_label=options.query_label,
        )


def _add_consolidation_steps(
    plan: SqlPlan,
    options: TransferOptions,
    stage_tables: list[str],
    *,
    lazy: bool,
) -> None:
    if options.write_mode == "upsert" or options.trino_mode == "parquet":
        return
    phase = "consolidate_stage_if_created" if lazy else "consolidate_stage"
    for worker_stage_table in stage_tables[1:]:
        add_insert_from_stage_step(
            plan,
            alias=options.to_db_key,
            backend=options.to_db_backend,
            target_table=stage_tables[0],
            stage_table=worker_stage_table,
            phase=phase,
            query_label=options.query_label,
        )


def _add_destination_steps(
    plan: SqlPlan,
    options: TransferOptions,
    stage_table: str,
    stage_tables: list[str],
) -> None:
    adapter = get_backend_adapter(options.to_db_backend)
    if options.write_mode == "replace":
        plan.extend(
            adapter.build_transfer_replace_target_sqls(
                options.target_table,
                query_label=options.query_label,
                ch_cluster=options.ch_cluster,
                ch_only_shard=options.ch_only_shard,
            ),
            alias=options.to_db_key,
            backend=options.to_db_backend,
            phase=adapter.transfer_replace_target_phase(),
            target_table=options.target_table,
        )
    elif options.write_mode == "truncate_insert":
        add_clear_target_steps(
            plan,
            alias=options.to_db_key,
            backend=options.to_db_backend,
            table_name=options.target_table,
            query_label=options.query_label,
            ch_cluster=options.ch_cluster,
            ch_only_shard=options.ch_only_shard,
        )
    if options.write_mode == "upsert":
        add_upsert_target_dry_run_steps(
            plan,
            options,
            stage_table=stage_table,
            stage_tables=stage_tables,
        )
    else:
        add_insert_target_dry_run_steps(plan, options, stage_table=stage_table)
    add_analyze_step(
        plan,
        alias=options.to_db_key,
        backend=options.to_db_backend,
        table_name=options.target_table,
        query_label=options.query_label,
    )
    add_count_step(
        plan,
        alias=options.to_db_key,
        backend=options.to_db_backend,
        table_name=options.target_table,
        query_label=options.query_label,
    )


def _add_target_cleanup_steps(
    plan: SqlPlan,
    options: TransferOptions,
    stage_tables: list[str],
    stage_external_location: str | None,
    *,
    lazy: bool,
) -> None:
    for worker_stage_table in stage_tables:
        _add_target_stage_cleanup(plan, options, worker_stage_table, lazy=lazy)
    target_adapter = get_backend_adapter(options.to_db_backend)
    if options.write_mode == "upsert" and target_adapter.uses_partition_replacement_upsert():
        _add_target_stage_cleanup(
            plan,
            options,
            dry_run_final_upsert_stage_table_name(options),
            lazy=False,
        )
    if options.trino_mode == "parquet":
        plan.add(
            f"DELETE STAGE FILES {stage_external_location or '<stage external location>'}",
            alias=options.to_db_key,
            backend=options.to_db_backend,
            phase="cleanup_stage",
            target_table=stage_tables[0],
        )


def _add_target_stage_cleanup(
    plan: SqlPlan,
    options: TransferOptions,
    stage_table: str,
    *,
    lazy: bool,
) -> None:
    phase = "drop_stage_if_created" if lazy else "drop_stage"
    cleanup_sqls = get_backend_adapter(
        options.to_db_backend
    ).build_creation_policy_cleanup_sqls(
        stage_table,
        options.staging_ch_policy,
        query_label=options.query_label,
    )
    if cleanup_sqls:
        plan.extend(
            cleanup_sqls,
            alias=options.to_db_key,
            backend=options.to_db_backend,
            phase=phase,
            target_table=stage_table,
        )
        return
    if lazy:
        plan.add(
            f"DROP TABLE IF EXISTS {stage_table}",
            alias=options.to_db_key,
            backend=options.to_db_backend,
            phase=phase,
            target_table=stage_table,
            query_label=options.query_label,
        )
        return
    add_cleanup_stage_step(
        plan,
        alias=options.to_db_key,
        backend=options.to_db_backend,
        stage_table=stage_table,
        query_label=options.query_label,
    )


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
                options.s3_transfer_staging_schema
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
    if _uses_lazy_keyed_source_staging(options):
        if worker_index is None:
            return "dynamically scheduled ready whole-key batches"
        return f"writer {worker_index} dynamically claimed ready whole-key batches"
    if worker_index is None or dry_run_worker_stage_count(options) == 1:
        return "shared keyed source slice batches"
    worker_count = dry_run_worker_stage_count(options)
    slice_indexes = [
        transfer_slice.index
        for transfer_slice in options.transfer_slices[worker_index::worker_count]
    ]
    return f"worker {worker_index} streamed keyed source slice batches {slice_indexes}"


def dry_run_reader_slice_assignments(options: TransferOptions) -> dict[int, list[int]] | None:
    if options.transfer_slices is None or _uses_lazy_keyed_source_staging(options):
        return None
    worker_count = options.transfer_concurrency.effective_read
    return {
        worker_index: [item.index for item in options.transfer_slices[worker_index::worker_count]]
        for worker_index in range(worker_count)
    }


def _uses_lazy_keyed_source_staging(options: TransferOptions) -> bool:
    return (
        options.transfer_slices is not None and options.source_transfer_staging_schema is not None
    )


def dry_run_stage_external_location(options: TransferOptions) -> str | None:
    if not options.s3_transfer_staging_location:
        return None
    try:
        return build_stage_external_location(
            options,
            stage_suffix=options.transfer_id or "<runtime-transfer-id>",
        )
    except Exception:
        return options.s3_transfer_staging_location.rstrip("/") + "/__stage__dryrun/"


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
