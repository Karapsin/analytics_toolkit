from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.ddl.api import _gp_partition_plan_option

from ....backends import get_backend_adapter
from ....connection.config import get_connection_config
from ....connection.errors import SqlOperationContext, sql_preview
from ....connection.get_sql_connection import get_sql_connection
from ....ddl.api import _build_create_table_sqls
from ....ddl.schema import normalize_table_schema
from ....execution.operation_runner import (
    run_annotated_once,
    run_retrying_operation,
    timed_public_sql_function,
    tracked_sql_operation,
)
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
from ....execution.plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
from ...ddl_options import resolve_operation_ddl
from ...load.load_sql_table import AmbiguousTableLoadError
from ...table._basic_ops import count_table_rows
from ...table.table_validation import normalize_key_columns, normalize_upsert_partition_column
from ..io.source import TransferSourceStreamReadError
from ..runtime.models import TransferOptions, TrinoTransferMode
from ..runtime.retry import run_with_retry
from ..staging import _sanitize_transfer_staging_username
from . import options as transfer_options
from .attempt import run_transfer_attempt
from .concurrency import resolve_transfer_concurrency
from .dry_run import (
    add_insert_target_dry_run_steps,
    add_upsert_target_dry_run_steps,
    dry_run_final_upsert_stage_table_name,
    dry_run_stage_external_location,
    dry_run_stage_table_names,
    dry_run_transfer_options,
    source_batches_label,
)
from .keys import normalize_transfer_slices
from .parquet_stage import build_create_parquet_stage_table_sql
from .row_counts import best_effort_transfer_target_count
from .runtime_identity import prepare_transfer_runtime
from .source import normalize_transfer_source
from .stage_identity import resolve_destination_identity
from .stream_retries import TransferStreamRetryState


@timed_public_sql_function
def transfer_table(
    from_db: str,
    to_db: str,
    from_sql: str | None = None,
    to_table: str | None = None,
    from_table: str | None = None,
    write_mode: str | None = "append",
    batch_size: int = 100_000,
    adaptive_batch_size: bool = True,
    min_batch_size: int = 1_000,
    max_batch_size: int | None = None,
    adaptive_batch_size_step: float = 0.1,
    target_rows_per_second: bool = True,
    target_batch_seconds: float | None = None,
    min_batch_seconds: float | None = None,
    max_batch_seconds: float | None = None,
    target_batch_memory_mb: float | None = None,
    min_batch_memory_mb: float | None = None,
    max_batch_memory_mb: float | None = None,
    target_rows_per_second_window: int = 5,
    target_rows_per_second_deadband: float = 0.15,
    retry_cnt: int = 5,
    timeout_increment: float = 5,
    full_retry_cnt: int = 5,
    full_timeout_increment: float = 60 * 10,
    key_columns: str | Sequence[str] | None = None,
    upsert_partition_column: str | None = None,
    gp_distributed_by_key: str | Sequence[str] | None = None,
    gp_partitions: Mapping[str, Any] | None = None,
    gp_insert_chunk_size: int | None = None,
    trino_insert_chunk_size: int | None = None,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str | None = None,
    ch_cluster: str | None = None,
    ch_sharding_key: str | None = None,
    ch_distributed_table: bool | None = None,
    ch_distributed_engine_template: str | None = None,
    ch_distributed_cluster: str | None = None,
    ch_shard_on_cluster: str | None = None,
    ch_distributed_on_cluster: str | None = None,
    ch_only_shard: bool = False,
    ch_retry_per_host_drops: bool = True,
    dry_run: bool = False,
    return_sql: bool = False,
    return_metadata: bool = False,
    query_label: str | None = None,
    progress: bool = False,
    estimate_total_rows: bool = False,
    table_schema: dict[str, str] | None = None,
    transfer_keys: str | Sequence[str] | Mapping[str, str] | None = None,
    transfer_key_values: Sequence[Any] | Mapping[str, Sequence[Any]] | None = None,
    concurrency: int | None = None,
    read_concurrency: int | None = None,
    write_concurrency: int | None = None,
    ignore_source_staging: bool = False,  # noqa: FBT002
    trino_mode: TrinoTransferMode | None = None,
    validate_row_count: bool = True,
    ch_count_limit_read: bool = True,
) -> int | SqlPlan | SqlOperationResult:
    options = build_transfer_options(
        from_db=from_db,
        to_db=to_db,
        from_sql=from_sql,
        to_table=to_table,
        from_table=from_table,
        write_mode=write_mode,
        batch_size=batch_size,
        adaptive_batch_size=adaptive_batch_size,
        min_batch_size=min_batch_size,
        max_batch_size=max_batch_size,
        adaptive_batch_size_step=adaptive_batch_size_step,
        target_rows_per_second=target_rows_per_second,
        target_batch_seconds=target_batch_seconds,
        min_batch_seconds=min_batch_seconds,
        max_batch_seconds=max_batch_seconds,
        target_batch_memory_mb=target_batch_memory_mb,
        min_batch_memory_mb=min_batch_memory_mb,
        max_batch_memory_mb=max_batch_memory_mb,
        target_rows_per_second_window=target_rows_per_second_window,
        target_rows_per_second_deadband=target_rows_per_second_deadband,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        full_retry_cnt=full_retry_cnt,
        full_timeout_increment=full_timeout_increment,
        key_columns=key_columns,
        upsert_partition_column=upsert_partition_column,
        gp_distributed_by_key=gp_distributed_by_key,
        gp_partitions=gp_partitions,
        gp_insert_chunk_size=gp_insert_chunk_size,
        trino_insert_chunk_size=trino_insert_chunk_size,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=ch_distributed_table,
        ch_distributed_engine_template=ch_distributed_engine_template,
        ch_distributed_cluster=ch_distributed_cluster,
        ch_shard_on_cluster=ch_shard_on_cluster,
        ch_distributed_on_cluster=ch_distributed_on_cluster,
        ch_only_shard=ch_only_shard,
        ch_retry_per_host_drops=ch_retry_per_host_drops,
        query_label=query_label,
        progress=progress,
        estimate_total_rows=estimate_total_rows,
        table_schema=table_schema,
        transfer_keys=transfer_keys,
        transfer_key_values=transfer_key_values,
        concurrency=concurrency,
        read_concurrency=read_concurrency,
        write_concurrency=write_concurrency,
        ignore_source_staging=ignore_source_staging,
        trino_mode=trino_mode,
        validate_row_count=validate_row_count,
        ch_count_limit_read=ch_count_limit_read,
    )

    if dry_run or return_sql:
        return build_transfer_table_plan(prepare_transfer_runtime(options, dry_run=True))

    options = prepare_transfer_runtime(options, dry_run=False)
    transfer_id = options.transfer_id or ""

    time_print(
        f"Starting table transfer from {options.from_db_key} "
        f"to {options.to_db_key}: {options.target_table} "
        f"(transfer_id={transfer_id})"
    )
    operation_metadata = SqlOperationMetadata(
        query_label=options.query_label,
        transfer_id=transfer_id,
        requested_read_concurrency=options.transfer_concurrency.requested_read,
        requested_write_concurrency=options.transfer_concurrency.requested_write,
        effective_read_concurrency=options.transfer_concurrency.effective_read,
        effective_write_concurrency=options.transfer_concurrency.effective_write,
        ignore_source_staging=options.ignore_source_staging,
        source_staging_mode=(
            "source_staged" if options.source_transfer_staging_schema else "direct"
        ),
        source_stage_count=(
            options.transfer_concurrency.effective_read
            if options.transfer_slices and options.source_transfer_staging_schema
            else int(options.source_transfer_staging_schema is not None)
        ),
    )
    stream_retry_state = TransferStreamRetryState(options)

    def transfer_operation(attempt: int) -> int:
        attempt_options = stream_retry_state.options_for_attempt()
        attempt_policy = get_backend_adapter(
            attempt_options.to_db_backend,
        ).transfer_attempt_policy(attempt_options.retry_cnt)
        with tracked_sql_operation(
            metadata=operation_metadata,
            operation_name="transfer_table",
            alias=attempt_options.to_db_key,
            backend=attempt_options.to_db_backend,
            phase="transfer",
            retry_attempt=attempt,
            query_label=attempt_options.query_label,
            preview_sql=attempt_options.source_sql,
        ):
            try:
                if not attempt_policy.retry_ambiguous_stage_load:
                    return run_transfer_attempt(
                        options=attempt_options,
                        read_retry_cnt=attempt_options.retry_cnt,
                        insert_retry_cnt=attempt_policy.insert_retry_cnt,
                    )

                def stage_restart_operation(inner_attempt: int) -> int:
                    del inner_attempt
                    try:
                        return run_transfer_attempt(
                            options=attempt_options,
                            read_retry_cnt=attempt_options.retry_cnt,
                            insert_retry_cnt=attempt_policy.insert_retry_cnt,
                        )
                    except AmbiguousTableLoadError as exc:
                        time_print(f"Discarding staged load and restarting from scratch: {exc!r}")
                        raise

                return run_with_retry(
                    operation_name=(
                        f"restarting staged transfer from {attempt_options.from_db_key} "
                        f"to {attempt_options.to_db_key}: "
                        f"{attempt_options.target_table}"
                    ),
                    retry_cnt=attempt_options.retry_cnt,
                    timeout_increment=attempt_options.timeout_increment,
                    operation=stage_restart_operation,
                    retryable_exceptions=(AmbiguousTableLoadError,),
                )
            except TransferSourceStreamReadError as exc:
                stream_retry_state.handle_failure(
                    exc,
                    attempt_options=attempt_options,
                    attempt=attempt,
                )
                raise

    def transfer_context(attempt: int) -> SqlOperationContext:
        return SqlOperationContext(
            operation="transfer_table",
            alias=options.to_db_key,
            backend=options.to_db_backend,
            phase="transfer",
            target_table=options.target_table,
            retry_attempt=attempt,
            sql_preview=sql_preview(options.source_sql),
        )

    if options.replace_target_table:
        total_rows = run_retrying_operation(
            operation_name=(
                f"restarting full transfer from {options.from_db_key} "
                f"to {options.to_db_key}: {options.target_table}"
            ),
            retry_cnt=options.full_retry_cnt,
            timeout_increment=options.full_timeout_increment,
            operation=transfer_operation,
            context_factory=transfer_context,
        )
    else:
        total_rows = run_annotated_once(
            operation=lambda: transfer_operation(1),
            context=transfer_context(1),
        )

    time_print(
        f"Finished table transfer from {options.from_db_key} "
        f"to {options.to_db_key}: {total_rows} row(s) "
        f"(transfer_id={transfer_id})"
    )
    if return_metadata:
        metadata = operation_metadata
        metadata.source_rows = total_rows
        if options.row_count_result is not None:
            metadata.expected_source_rows = options.row_count_result.expected_source_rows
            metadata.streamed_rows = options.row_count_result.streamed_rows
            metadata.stage_rows = options.row_count_result.stage_rows
            metadata.row_count_validated = options.row_count_result.row_count_validated
            metadata.transfer_slice_counts = options.row_count_result.slice_counts_as_dicts()
        metadata.staged_rows = total_rows
        metadata.inserted_rows = total_rows
        metadata.affected_rows = total_rows
        metadata.final_target_rows = best_effort_transfer_target_count(
            options,
            open_connection=get_sql_connection,
            count_rows=count_table_rows,
        )
        return SqlOperationResult(
            rows=total_rows,
            metadata=metadata,
        )
    return total_rows


def build_transfer_options(
    from_db: str,
    to_db: str,
    from_sql: str | None = None,
    to_table: str | None = None,
    from_table: str | None = None,
    write_mode: str | None = "append",
    batch_size: int = 100_000,
    adaptive_batch_size: bool = True,
    min_batch_size: int = 1_000,
    max_batch_size: int | None = None,
    adaptive_batch_size_step: float = 0.1,
    target_rows_per_second: bool = True,
    target_batch_seconds: float | None = None,
    min_batch_seconds: float | None = None,
    max_batch_seconds: float | None = None,
    target_batch_memory_mb: float | None = None,
    min_batch_memory_mb: float | None = None,
    max_batch_memory_mb: float | None = None,
    target_rows_per_second_window: int = 5,
    target_rows_per_second_deadband: float = 0.15,
    retry_cnt: int = 5,
    timeout_increment: float = 5,
    full_retry_cnt: int = 5,
    full_timeout_increment: float = 60 * 10,
    key_columns: str | Sequence[str] | None = None,
    upsert_partition_column: str | None = None,
    gp_distributed_by_key: str | Sequence[str] | None = None,
    gp_partitions: Mapping[str, Any] | None = None,
    gp_insert_chunk_size: int | None = None,
    trino_insert_chunk_size: int | None = None,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str | None = None,
    ch_cluster: str | None = None,
    ch_sharding_key: str | None = None,
    ch_distributed_table: bool | None = None,
    ch_distributed_engine_template: str | None = None,
    ch_distributed_cluster: str | None = None,
    ch_shard_on_cluster: str | None = None,
    ch_distributed_on_cluster: str | None = None,
    ch_only_shard: bool = False,
    ch_retry_per_host_drops: bool = True,
    query_label: str | None = None,
    progress: bool = False,
    estimate_total_rows: bool = False,
    table_schema: dict[str, str] | None = None,
    transfer_keys: str | Sequence[str] | Mapping[str, str] | None = None,
    transfer_key_values: Sequence[Any] | Mapping[str, Sequence[Any]] | None = None,
    concurrency: int | None = None,
    read_concurrency: int | None = None,
    write_concurrency: int | None = None,
    ignore_source_staging: bool = False,  # noqa: FBT002
    trino_mode: TrinoTransferMode | None = None,
    validate_row_count: bool = True,
    ch_count_limit_read: bool = True,
) -> TransferOptions:
    transfer_options.validate_ignore_source_staging(ignore_source_staging)
    transfer_options.validate_transfer_runtime_options(
        batch_size=batch_size,
        min_batch_size=min_batch_size,
        max_batch_size=max_batch_size,
        adaptive_batch_size_step=adaptive_batch_size_step,
        target_batch_seconds=target_batch_seconds,
        min_batch_seconds=min_batch_seconds,
        max_batch_seconds=max_batch_seconds,
        target_batch_memory_mb=target_batch_memory_mb,
        min_batch_memory_mb=min_batch_memory_mb,
        max_batch_memory_mb=max_batch_memory_mb,
        target_rows_per_second_window=target_rows_per_second_window,
        target_rows_per_second_deadband=target_rows_per_second_deadband,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        full_retry_cnt=full_retry_cnt,
        full_timeout_increment=full_timeout_increment,
        gp_insert_chunk_size=gp_insert_chunk_size,
        trino_insert_chunk_size=trino_insert_chunk_size,
        concurrency=concurrency,
        read_concurrency=read_concurrency,
        write_concurrency=write_concurrency,
    )
    source_sql, source_table = normalize_transfer_source(
        from_sql=from_sql,
        from_table=from_table,
    )
    from_config = get_connection_config(from_db)
    to_config = get_connection_config(to_db)
    source_staging_schema = None if ignore_source_staging else from_config.transfer_staging_schema
    target_adapter = get_backend_adapter(to_config.backend)
    target_defaults = target_adapter.target_connection_defaults(to_config)
    resolved_trino_mode = target_adapter.resolve_transfer_staging_mode(
        trino_mode,
        transfer_staging_schema=to_config.transfer_staging_schema,
        transfer_staging_location=target_defaults.transfer_staging_location,
    )
    resolved_write_mode = transfer_options.resolve_transfer_write_mode(
        to_config.backend, write_mode
    )
    resolved_target_rows_per_second = transfer_options.resolve_target_adaptation_mode(
        adaptive_batch_size=adaptive_batch_size,
        target_rows_per_second=target_rows_per_second,
        target_batch_seconds=target_batch_seconds,
        target_batch_memory_mb=target_batch_memory_mb,
    )
    (
        resolved_target_batch_memory_mb,
        resolved_target_batch_memory_bytes,
    ) = transfer_options.resolve_target_batch_memory(target_batch_memory_mb)
    (
        resolved_min_batch_memory_mb,
        resolved_min_batch_memory_bytes,
        resolved_max_batch_memory_mb,
        resolved_max_batch_memory_bytes,
    ) = transfer_options.resolve_target_batch_memory_limits(
        min_batch_memory_mb=min_batch_memory_mb,
        max_batch_memory_mb=max_batch_memory_mb,
    )
    (
        resolved_min_batch_size,
        resolved_max_batch_size,
        resolved_target_batch_seconds,
        resolved_min_batch_seconds,
        resolved_max_batch_seconds,
    ) = transfer_options.resolve_adaptive_batch_bounds(
        batch_size=batch_size,
        min_batch_size=min_batch_size,
        max_batch_size=max_batch_size,
        target_batch_seconds=target_batch_seconds,
        min_batch_seconds=min_batch_seconds,
        max_batch_seconds=max_batch_seconds,
        adaptive_batch_size=adaptive_batch_size,
        unlimited_default_max=resolved_target_batch_memory_bytes is not None,
    )
    resolved_target_rows_per_second_window = transfer_options.resolve_target_rows_per_second_window(
        target_rows_per_second_window,
    )
    resolved_target_rows_per_second_deadband = (
        transfer_options.resolve_target_rows_per_second_deadband(target_rows_per_second_deadband)
    )
    resolved_adaptive_batch_size_step = transfer_options.resolve_adaptive_batch_size_step(
        adaptive_batch_size_step,
    )
    retry_per_host_drops = target_adapter.resolve_ch_retry_per_host_drops(
        bool(ch_retry_per_host_drops)
    )
    (
        normalized_transfer_keys,
        normalized_transfer_key_expressions,
        normalized_transfer_key_values,
        transfer_slices,
        normalized_legacy_concurrency,
    ) = normalize_transfer_slices(
        source_sql=source_sql,
        source_table=source_table,
        transfer_keys=transfer_keys,
        transfer_key_values=transfer_key_values,
        concurrency=concurrency,
        allow_unkeyed_concurrency=source_staging_schema is not None,
    )
    resolved_concurrency = resolve_transfer_concurrency(
        concurrency=concurrency,
        read_concurrency=read_concurrency,
        write_concurrency=write_concurrency,
        slice_count=len(transfer_slices) if transfer_slices is not None else None,
        direct_keyed=transfer_slices is not None,
    )
    destination_identity = resolve_destination_identity(
        to_table.strip() if isinstance(to_table, str) and to_table.strip() else "_invalid_",
        to_config.backend,
    )
    normalized_partition_by = target_adapter.normalize_ch_columns_or_expression(
        partition_by,
        "partition_by",
    )
    normalized_gp_partitions = target_adapter.normalize_gp_partitions_option(
        gp_partitions,
        partition_by=normalized_partition_by,
        option_owner="to_db",
    )
    ddl = resolve_operation_ddl(
        to_config,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=ch_distributed_table,
        ch_only_shard=ch_only_shard,
        ch_distributed_engine_template=ch_distributed_engine_template,
        ch_distributed_cluster=ch_distributed_cluster,
        ch_shard_on_cluster=ch_shard_on_cluster,
        ch_distributed_on_cluster=ch_distributed_on_cluster,
    )
    options = TransferOptions(
        from_db_key=from_config.connection_key,
        from_db_backend=from_config.backend,
        to_db_key=to_config.connection_key,
        to_db_backend=to_config.backend,
        source_sql=source_sql,
        source_table=source_table,
        target_table=to_table.strip() if isinstance(to_table, str) else "",
        canonical_destination_identity=destination_identity.canonical,
        full_destination_fingerprint=destination_identity.fingerprint,
        destination_hash=destination_identity.hash_prefix,
        table_schema=normalize_table_schema(table_schema),
        replace_target_table=resolved_write_mode != "append",
        write_mode=resolved_write_mode,
        batch_size=batch_size,
        adaptive_batch_size=adaptive_batch_size,
        min_batch_size=resolved_min_batch_size,
        max_batch_size=resolved_max_batch_size,
        adaptive_batch_size_step=resolved_adaptive_batch_size_step,
        target_rows_per_second=resolved_target_rows_per_second,
        target_batch_seconds=resolved_target_batch_seconds,
        min_batch_seconds=resolved_min_batch_seconds,
        max_batch_seconds=resolved_max_batch_seconds,
        target_batch_memory_mb=resolved_target_batch_memory_mb,
        target_batch_memory_bytes=resolved_target_batch_memory_bytes,
        min_batch_memory_mb=resolved_min_batch_memory_mb,
        min_batch_memory_bytes=resolved_min_batch_memory_bytes,
        max_batch_memory_mb=resolved_max_batch_memory_mb,
        max_batch_memory_bytes=resolved_max_batch_memory_bytes,
        target_rows_per_second_window=resolved_target_rows_per_second_window,
        target_rows_per_second_deadband=resolved_target_rows_per_second_deadband,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        full_retry_cnt=full_retry_cnt,
        full_timeout_increment=full_timeout_increment,
        key_columns=normalize_key_columns(key_columns),
        upsert_partition_column=normalize_upsert_partition_column(upsert_partition_column),
        trino_upsert_partition_drop_sql_template=(
            target_defaults.upsert_partition_drop_sql_template
        ),
        gp_distributed_by_key=normalize_key_columns(
            gp_distributed_by_key,
            "gp_distributed_by_key",
        ),
        gp_partitions=normalized_gp_partitions,
        gp_insert_chunk_size=gp_insert_chunk_size,
        trino_insert_chunk_size=(
            trino_insert_chunk_size
            if trino_insert_chunk_size is not None
            else target_defaults.insert_chunk_size
        ),
        partition_by=normalized_partition_by,
        order_by=target_adapter.normalize_ch_columns_or_expression(
            order_by,
            "order_by",
        ),
        ch_engine=(
            ddl.regular_ch_policy.shard_engine
            if ddl.regular_ch_policy
            else ch_engine or "ReplicatedMergeTree"
        ),
        ch_cluster=(
            ddl.regular_ch_policy.distributed_cluster
            or ddl.regular_ch_policy.shard_on_cluster
            or "{cluster}"
            if ddl.regular_ch_policy
            else ch_cluster or "{cluster}"
        ),
        ch_sharding_key=(
            ddl.regular_ch_policy.sharding_key or "rand()"
            if ddl.regular_ch_policy
            else ch_sharding_key or "rand()"
        ),
        ch_only_shard=transfer_options.normalize_only_shard(ch_only_shard),
        ch_retry_per_host_drops=retry_per_host_drops,
        transfer_staging_schema=to_config.transfer_staging_schema,
        source_transfer_staging_schema=source_staging_schema,
        source_transfer_staging_username=_sanitize_transfer_staging_username(from_config.user),
        ignore_source_staging=ignore_source_staging,
        transfer_parquet_staging_schema=getattr(
            to_config,
            "transfer_parquet_staging_schema",
            None,
        ),
        transfer_staging_location=target_defaults.transfer_staging_location,
        transfer_staging_username=_sanitize_transfer_staging_username(to_config.user),
        trino_mode=resolved_trino_mode,
        transfer_keys=normalized_transfer_keys,
        transfer_key_expressions=normalized_transfer_key_expressions,
        transfer_key_values=normalized_transfer_key_values,
        transfer_slices=transfer_slices,
        concurrency=normalized_legacy_concurrency,
        transfer_concurrency=resolved_concurrency,
        query_label=query_label,
        progress=progress,
        estimate_total_rows=estimate_total_rows,
        validate_row_count=validate_row_count,
        ch_count_limit_read=ch_count_limit_read,
        regular_ddl_properties=ddl.regular_properties,
        staging_ddl_properties=ddl.staging_properties,
        parquet_ddl_properties=ddl.parquet_properties,
        regular_ch_policy=ddl.regular_ch_policy,
        staging_ch_policy=ddl.staging_ch_policy,
    )

    transfer_options.validate_built_transfer_options(options, target_adapter)
    return options


def build_transfer_table_plan(options: TransferOptions) -> SqlPlan:
    target_adapter = get_backend_adapter(options.to_db_backend)
    uses_partition_replacement_upsert = target_adapter.uses_partition_replacement_upsert()
    stage_tables = dry_run_stage_table_names(options)
    stage_table = stage_tables[0]
    insert_page_sizing = target_adapter.transfer_insert_page_sizing(
        gp_insert_chunk_size=options.gp_insert_chunk_size
    )
    stage_external_location = (
        dry_run_stage_external_location(options) if options.trino_mode == "parquet" else None
    )
    plan = SqlPlan(
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
            effective_read_concurrency=options.transfer_concurrency.effective_read,
            effective_write_concurrency=options.transfer_concurrency.effective_write,
            ignore_source_staging=options.ignore_source_staging,
            source_staging_mode=(
                "source_staged" if options.source_transfer_staging_schema else "direct"
            ),
            source_stage_count=(
                options.transfer_concurrency.effective_read
                if options.transfer_slices and options.source_transfer_staging_schema
                else int(options.source_transfer_staging_schema is not None)
            ),
        ),
    )
    if options.transfer_slices is None:
        plan.add(
            options.source_sql,
            alias=options.from_db_key,
            backend=options.from_db_backend,
            phase="read_source",
            query_label=options.query_label,
        )
    else:
        for transfer_slice in options.transfer_slices:
            plan.add(
                transfer_slice.source_sql,
                alias=options.from_db_key,
                backend=options.from_db_backend,
                phase=(
                    "materialize_source_stage"
                    if options.source_transfer_staging_schema
                    else "read_source"
                ),
                query_label=options.query_label,
            )
    if options.trino_mode == "parquet":
        plan.add(
            build_create_parquet_stage_table_sql(
                stage_table,
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
            phase="create_stage",
            target_table=stage_table,
        )
    elif options.table_schema is None:
        for worker_stage_table in stage_tables:
            add_create_table_placeholder_step(
                plan,
                alias=options.to_db_key,
                backend=options.to_db_backend,
                phase="create_stage",
                table_name=worker_stage_table,
                query_label=options.query_label,
            )
    else:
        for worker_stage_table in stage_tables:
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
                phase="create_stage",
                table_name=worker_stage_table,
            )
    if options.trino_mode == "parquet":
        add_load_stage_step(
            plan,
            alias=options.to_db_key,
            backend=options.to_db_backend,
            stage_table=stage_table,
            sql=(
                "WRITE PARQUET FILES TO "
                f"{stage_external_location or '<stage external location>'} "
                f"FROM <{source_batches_label(options)}>"
            ),
            query_label=options.query_label,
        )
    else:
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
        for worker_stage_table in stage_tables[1:]:
            add_insert_from_stage_step(
                plan,
                alias=options.to_db_key,
                backend=options.to_db_backend,
                target_table=stage_table,
                stage_table=worker_stage_table,
                phase="consolidate_stage",
                query_label=options.query_label,
            )
    if options.write_mode == "replace":
        adapter = get_backend_adapter(options.to_db_backend)
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
        add_insert_target_dry_run_steps(
            plan,
            stage_table=stage_table,
            options=options,
        )
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
    for worker_stage_table in stage_tables:
        add_cleanup_stage_step(
            plan,
            alias=options.to_db_key,
            backend=options.to_db_backend,
            stage_table=worker_stage_table,
            query_label=options.query_label,
        )
    if options.write_mode == "upsert" and uses_partition_replacement_upsert:
        add_cleanup_stage_step(
            plan,
            alias=options.to_db_key,
            backend=options.to_db_backend,
            stage_table=dry_run_final_upsert_stage_table_name(options),
            query_label=options.query_label,
        )
    if options.trino_mode == "parquet":
        plan.add(
            f"DELETE STAGE FILES {stage_external_location or '<stage external location>'}",
            alias=options.to_db_key,
            backend=options.to_db_backend,
            phase="cleanup_stage",
            target_table=stage_table,
        )
    return plan
