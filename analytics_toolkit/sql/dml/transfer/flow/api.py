from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.backends.trino.storage import parquet_storage_options

from ....backends import get_backend_adapter
from ....connection.config import get_connection_config
from ....connection.get_sql_connection import get_sql_connection
from ....ddl.schema import normalize_table_schema
from ....execution.operation_runner import (
    run_annotated_once,
    run_retrying_operation,
    timed_public_sql_function,
    tracked_sql_operation,
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
from .dry_run import build_transfer_table_plan as build_dry_run_transfer_table_plan
from .keys import normalize_transfer_slices
from .logging import TransferAttemptLogState, build_transfer_operation_context
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
    ch_ddl_ready_timeout_seconds: float | None = None,
    ch_ddl_ready_timeout_extension_cnt: int | None = None,
    ch_ddl_wait_policy: str | None = None,
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
    soft_concurrency_cap: int | None = None,
    hard_concurrency_cap: int = 5,
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
        ch_ddl_ready_timeout_seconds=ch_ddl_ready_timeout_seconds,
        ch_ddl_ready_timeout_extension_cnt=ch_ddl_ready_timeout_extension_cnt,
        ch_ddl_wait_policy=ch_ddl_wait_policy,
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
        soft_concurrency_cap=soft_concurrency_cap,
        hard_concurrency_cap=hard_concurrency_cap,
        ignore_source_staging=ignore_source_staging,
        trino_mode=trino_mode,
        validate_row_count=validate_row_count,
        ch_count_limit_read=ch_count_limit_read,
        collect_final_target_count=return_metadata,
    )

    if dry_run or return_sql:
        return build_transfer_table_plan(prepare_transfer_runtime(options, dry_run=True))

    options = prepare_transfer_runtime(options, dry_run=False)
    transfer_id = options.transfer_id or ""
    source_staged_transfer = options.source_transfer_staging_schema is not None
    lazy_keyed_source_staging = (
        options.transfer_slices is not None and options.source_transfer_staging_schema is not None
    )

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
            len(options.transfer_slices)
            if options.transfer_slices and options.source_transfer_staging_schema
            else int(options.source_transfer_staging_schema is not None)
        ),
        live_source_stage_limit=(
            options.transfer_concurrency.effective_read
            + options.transfer_concurrency.effective_write
            if options.transfer_slices and options.source_transfer_staging_schema
            else None
        ),
    )
    stream_retry_state = TransferStreamRetryState(options)
    completed_attempt_options = options
    attempt_log_state = TransferAttemptLogState()

    def transfer_operation(attempt: int) -> int:
        nonlocal completed_attempt_options
        attempt_options = replace(
            stream_retry_state.options_for_attempt(),
            attempt_number=attempt,
        )
        if attempt > 1:
            time_print(attempt_log_state.retry_message(attempt, options.full_retry_cnt))
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
            preview_sql=None if source_staged_transfer else attempt_options.source_sql,
        ):
            try:
                if lazy_keyed_source_staging or not attempt_policy.retry_ambiguous_stage_load:
                    result = attempt_log_state.run(
                        lambda: run_transfer_attempt(
                            options=attempt_options,
                            read_retry_cnt=attempt_options.retry_cnt,
                            insert_retry_cnt=attempt_policy.insert_retry_cnt,
                        )
                    )
                    completed_attempt_options = attempt_options
                    return result

                def stage_restart_operation(inner_attempt: int) -> int:
                    del inner_attempt
                    try:
                        return attempt_log_state.run(
                            lambda: run_transfer_attempt(
                                options=attempt_options,
                                read_retry_cnt=attempt_options.retry_cnt,
                                insert_retry_cnt=attempt_policy.insert_retry_cnt,
                            )
                        )
                    except AmbiguousTableLoadError as exc:
                        detail = type(exc).__name__ if source_staged_transfer else repr(exc)
                        time_print(f"Discarding staged load and restarting from scratch: {detail}")
                        raise

                result = run_with_retry(
                    operation_name=(
                        f"restarting staged transfer from {attempt_options.from_db_key} "
                        f"to {attempt_options.to_db_key}: "
                        f"{attempt_options.target_table}"
                    ),
                    retry_cnt=attempt_options.retry_cnt,
                    timeout_increment=attempt_options.timeout_increment,
                    operation=stage_restart_operation,
                    retryable_exceptions=(AmbiguousTableLoadError,),
                    safe_exception_logging=source_staged_transfer,
                )
                completed_attempt_options = attempt_options
                return int(result)
            except TransferSourceStreamReadError as exc:
                stream_retry_state.handle_failure(
                    exc,
                    attempt_options=attempt_options,
                    attempt=attempt,
                )
                raise

    def transfer_context(attempt: int) -> Any:
        return build_transfer_operation_context(options, attempt)

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
            safe_exception_logging=source_staged_transfer,
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
        return _build_transfer_metadata_result(
            total_rows=total_rows,
            options=options,
            completed_attempt_options=completed_attempt_options,
            metadata=operation_metadata,
        )
    return total_rows


def _build_transfer_metadata_result(
    *,
    total_rows: int,
    options: TransferOptions,
    completed_attempt_options: TransferOptions,
    metadata: SqlOperationMetadata,
) -> SqlOperationResult:
    runtime_concurrency = completed_attempt_options.transfer_concurrency
    metadata.effective_read_concurrency = runtime_concurrency.effective_read
    metadata.effective_write_concurrency = runtime_concurrency.effective_write
    metadata.source_rows = total_rows
    row_count_result = completed_attempt_options.row_count_result
    if row_count_result is not None:
        metadata.expected_source_rows = row_count_result.expected_source_rows
        metadata.streamed_rows = row_count_result.streamed_rows
        metadata.stage_rows = row_count_result.stage_rows
        metadata.row_count_validated = row_count_result.row_count_validated
        metadata.transfer_slice_counts = row_count_result.slice_counts_as_dicts()
    metadata.staged_rows = total_rows
    metadata.inserted_rows = total_rows
    metadata.affected_rows = total_rows
    lazy_keyed_source_staging = (
        completed_attempt_options.transfer_slices is not None
        and completed_attempt_options.source_transfer_staging_schema is not None
    )
    metadata.final_target_rows = (
        completed_attempt_options.final_target_rows
        if lazy_keyed_source_staging
        else best_effort_transfer_target_count(
            options,
            open_connection=get_sql_connection,
            count_rows=count_table_rows,
        )
    )
    return SqlOperationResult(rows=total_rows, metadata=metadata)


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
    ch_ddl_ready_timeout_seconds: float | None = None,
    ch_ddl_ready_timeout_extension_cnt: int | None = None,
    ch_ddl_wait_policy: str | None = None,
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
    soft_concurrency_cap: int | None = None,
    hard_concurrency_cap: int = 5,
    *,
    collect_final_target_count: bool = False,
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
        ch_ddl_ready_timeout_extension_cnt=ch_ddl_ready_timeout_extension_cnt,
        gp_insert_chunk_size=gp_insert_chunk_size,
        trino_insert_chunk_size=trino_insert_chunk_size,
        concurrency=concurrency,
        read_concurrency=read_concurrency,
        write_concurrency=write_concurrency,
        soft_concurrency_cap=soft_concurrency_cap,
        hard_concurrency_cap=hard_concurrency_cap,
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
        s3_transfer_staging_schema=getattr(
            to_config, "s3_transfer_staging_schema", None
        ),
        s3_transfer_staging_location=target_defaults.s3_transfer_staging_location,
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
        soft_concurrency_cap=soft_concurrency_cap,
        hard_concurrency_cap=hard_concurrency_cap,
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
        ch_ddl_ready_timeout_seconds=ch_ddl_ready_timeout_seconds,
        ch_ddl_ready_timeout_extension_cnt=ch_ddl_ready_timeout_extension_cnt,
        ch_ddl_wait_policy=ch_ddl_wait_policy,
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
        ch_ddl_ready_timeout_extension_cnt=(
            ddl.regular_ch_policy.ddl_ready_timeout_extension_cnt
            if ddl.regular_ch_policy is not None
            else 1
        ),
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
        s3_transfer_staging_schema=getattr(
            to_config,
            "s3_transfer_staging_schema",
            None,
        ),
        s3_transfer_staging_location=target_defaults.s3_transfer_staging_location,
        parquet_storage_options=parquet_storage_options(to_config),
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
        collect_final_target_count=collect_final_target_count,
        regular_ddl_properties=ddl.regular_properties,
        staging_ddl_properties=ddl.staging_properties,
        parquet_ddl_properties=ddl.parquet_properties,
        regular_ch_policy=ddl.regular_ch_policy,
        staging_ch_policy=ddl.staging_ch_policy,
    )

    transfer_options.validate_built_transfer_options(options, target_adapter)
    return options


def build_transfer_table_plan(options: TransferOptions) -> SqlPlan:
    return build_dry_run_transfer_table_plan(options)
