from __future__ import annotations

from sqlglot import exp, parse_one
import pandas as pd

from ....backends import get_backend_adapter
from ....ddl.api import _build_create_table_sqls
from ....core.identifiers import sqlglot_dialect
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
from .parquet_stage import build_stage_external_location
from ..runtime.models import TransferOptions


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
    if options.transfer_slices is None or options.source_transfer_staging_schema is not None:
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
