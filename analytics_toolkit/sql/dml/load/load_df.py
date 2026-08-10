from __future__ import annotations

# ruff: noqa: FBT001, I001, TID252

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import pandas as pd

from analytics_toolkit.sql.backends.trino.storage import parquet_storage_options
from tqdm import tqdm

from analytics_toolkit.general import time_print

if TYPE_CHECKING:
    from collections.abc import Mapping

from analytics_toolkit.sql.ddl.api import _gp_partition_plan_option
from analytics_toolkit.sql.dml.ddl_options import resolve_operation_ddl

from ...backends import get_backend_adapter
from ...connection.config import get_connection_config
from ...connection.errors import (
    SqlOperationContext,
    sql_preview,
)
from ...connection.get_sql_connection import get_sql_connection
from ...ddl.api import (
    _build_create_table_sqls,
    _create_sql_table_with_connection,
)
from ...ddl.identifiers import column_list_sql
from ...ddl.schema import (
    normalize_table_schema,
    validate_table_schema_columns,
)
from ...execution.operation_runner import (
    run_retrying_operation,
    timed_public_sql_function,
    tracked_sql_operation,
    validate_progress_option,
    validate_retry_options,
)
from ...execution.plan_steps import (
    add_analyze_step,
    add_cleanup_stage_step,
    add_clear_target_steps,
    add_count_step,
    add_create_table_steps,
    add_drop_target_steps,
    add_insert_from_stage_step,
    add_load_stage_step,
)
from ...execution.plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
from ..transfer.flow.stage_identity import resolve_destination_identity
from ...execution.validation import validate_optional_positive_int
from ..table._basic_ops import (
    count_table_rows,
    get_table_column_types,
    insert_from_table,
    table_exists,
)
from ..table.maintenance import (
    analyze_table,
    drop_table_with_retry,
)
from ..table.table_validation import (
    normalize_key_columns,
    normalize_upsert_partition_column,
    validate_key_columns_in_columns,
    validate_stage_target_key_overlap,
    validate_stage_uniqueness,
    validate_upsert_partition_column_in_columns,
)
from ..table.write_modes import (
    apply_target_write_mode,
    build_upsert_stage_sqls,
    upsert_stage_table,
)
from ..transfer.flow.parquet_stage import (
    PARQUET_STAGE_DEFAULT_MAX_ROW_GROUP_SIZE,
    build_create_parquet_stage_table_sql,
    build_stage_external_location,
    cleanup_parquet_stage_location,
    ensure_parquet_staging_dependencies,
    write_dataframe_to_parquet_stage,
)
from ..transfer.runtime.retry import (
    replace_connection,
    rollback_quietly,
    run_with_fresh_connection,
    run_with_retry,
)
from ..transfer.staging import _sanitize_transfer_staging_username
from .load_sql_table import insert_table_batch
from .models import LoadOptions, LoadState
from .stage import (
    STAGE_TABLE_NAME_MAX_ATTEMPTS,
    build_stage_table_name,
    cleanup_stage_table_with_retry,
    create_stage_table,
)


@timed_public_sql_function
def load_df(
    db_key: str,
    destination_table: str,
    df: pd.DataFrame,
    append: bool = False,
    write_mode: str | None = None,
    gp_distributed_by_key: str | Sequence[str] | None = None,
    gp_partitions: Mapping[str, Any] | None = None,
    key_columns: str | Sequence[str] | None = None,
    upsert_partition_column: str | None = None,
    retry_cnt: int = 5,
    timeout_increment: float = 5,
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
    ch_ddl_wait_policy: str | None = None,
    ch_only_shard: bool = False,
    ch_retry_per_host_drops: bool = True,
    dry_run: bool = False,
    return_sql: bool = False,
    return_metadata: bool = False,
    query_label: str | None = None,
    gp_insert_chunk_size: int | None = None,
    progress: bool = False,
    table_schema: dict[str, str] | None = None,
) -> int | SqlPlan | SqlOperationResult:
    if not isinstance(df, pd.DataFrame):
        raise TypeError("df must be a pandas DataFrame.")
    validate_retry_options(retry_cnt, timeout_increment)
    validate_optional_positive_int(
        trino_insert_chunk_size,
        "trino_insert_chunk_size",
    )
    validate_optional_positive_int(
        gp_insert_chunk_size,
        "gp_insert_chunk_size",
    )
    _validate_progress(progress)

    options = _build_load_options(
        db_key=db_key,
        destination_table=destination_table,
        append=append,
        write_mode=write_mode,
        gp_distributed_by_key=gp_distributed_by_key,
        gp_partitions=gp_partitions,
        key_columns=key_columns,
        upsert_partition_column=upsert_partition_column,
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
        ch_ddl_wait_policy=ch_ddl_wait_policy,
        ch_only_shard=ch_only_shard,
        ch_retry_per_host_drops=ch_retry_per_host_drops,
        query_label=query_label,
        gp_insert_chunk_size=gp_insert_chunk_size,
        table_schema=table_schema,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
    )

    if dry_run or return_sql:
        return build_load_df_plan(options, df)

    operation_metadata = SqlOperationMetadata(
        source_rows=len(df),
        query_label=options.query_label,
    )
    preview_sql = _load_df_preview_sql(options, df)

    def operation(attempt: int) -> int | SqlOperationResult:
        state: LoadState | None = None
        operation_error: Exception | None = None
        try:
            with tracked_sql_operation(
                metadata=operation_metadata,
                operation_name="load_df",
                alias=options.connection_key,
                backend=options.connection_backend,
                phase="load",
                retry_attempt=attempt,
                query_label=options.query_label,
                preview_sql=preview_sql,
            ):
                state = _run_load_target_action(
                    options,
                    "target_state",
                    lambda connection_ref: _initialize_load_state(
                        options,
                        connection_ref["connection"],
                    ),
                )
                if df.empty:
                    return _handle_empty_dataframe_load(
                        options,
                        state,
                        operation_metadata=operation_metadata,
                        return_metadata=return_metadata,
                    )

                _validate_load_dataframe(options, df)
                _run_load_target_action(
                    options,
                    "prepare_target",
                    lambda connection_ref: _prepare_load_target(
                        options=options,
                        state=state,
                        connection=connection_ref["connection"],
                        df=df,
                    ),
                )

                progress_bar = _make_load_progress_bar(
                    total=len(df),
                    options=options,
                    progress=progress,
                )
                progress_tracker = _ProgressTracker(progress_bar)
                try:
                    inserted_rows = _load_dataframe(
                        options=options,
                        state=state,
                        df=df,
                        on_progress=progress_tracker.update,
                    )
                    progress_tracker.complete_to(inserted_rows)
                finally:
                    progress_bar.close()

                _run_load_target_action(
                    options,
                    "analyze_target",
                    lambda connection_ref: _analyze_load_target(
                        options,
                        connection_ref["connection"],
                    ),
                )
                time_print(
                    f"Finished loading DataFrame into "
                    f"{options.destination_table}: "
                    f"{inserted_rows} row(s)"
                )
                return _build_load_result(
                    options=options,
                    state=state,
                    source_rows=len(df),
                    inserted_rows=inserted_rows,
                    operation_metadata=operation_metadata,
                    return_metadata=return_metadata,
                )
        except Exception as exc:
            operation_error = exc
            raise
        finally:
            _cleanup_load(
                options,
                state,
                drop_created_target=operation_error is not None,
            )

    def context(attempt: int) -> SqlOperationContext:
        return SqlOperationContext(
            operation="load_df",
            alias=options.connection_key,
            backend=options.connection_backend,
            phase="load",
            target_table=options.destination_table,
            retry_attempt=attempt,
            sql_preview=sql_preview(options.destination_table),
        )

    return run_retrying_operation(
        operation_name=(
            f"loading DataFrame into {options.connection_key}.{options.destination_table}"
        ),
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        operation=operation,
        context_factory=context,
    )


def _build_load_options(
    db_key: str,
    destination_table: str,
    append: bool,
    write_mode: str | None,
    gp_distributed_by_key: str | Sequence[str] | None,
    key_columns: str | Sequence[str] | None,
    gp_partitions: Mapping[str, Any] | None = None,
    upsert_partition_column: str | None = None,
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
    ch_ddl_wait_policy: str | None = None,
    ch_only_shard: bool = False,
    ch_retry_per_host_drops: bool = True,
    query_label: str | None = None,
    gp_insert_chunk_size: int | None = None,
    table_schema: dict[str, str] | None = None,
    retry_cnt: int = 5,
    timeout_increment: float = 5,
) -> LoadOptions:
    config = get_connection_config(db_key)
    adapter = get_backend_adapter(config.backend)
    target_defaults = adapter.target_connection_defaults(config)
    resolved_write_mode = _resolve_load_write_mode(
        config.backend,
        append=append,
        write_mode=write_mode,
    )
    retry_per_host_drops = adapter.resolve_ch_retry_per_host_drops(bool(ch_retry_per_host_drops))
    normalized_partition_by = adapter.normalize_ch_columns_or_expression(
        partition_by,
        "partition_by",
    )
    normalized_gp_partitions = adapter.normalize_gp_partitions_option(
        gp_partitions,
        partition_by=normalized_partition_by,
        option_owner="db_key",
    )
    ddl = resolve_operation_ddl(
        config,
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
        ch_ddl_wait_policy=ch_ddl_wait_policy,
    )
    resolved_destination_table = destination_table.strip()
    if not resolved_destination_table:
        raise ValueError("destination_table must not be empty.")
    options = LoadOptions(
        connection_key=config.connection_key,
        connection_backend=config.backend,
        destination_table=resolved_destination_table,
        destination_hash=resolve_destination_identity(
            resolved_destination_table,
            config.backend,
        ).hash_prefix,
        table_schema=normalize_table_schema(table_schema),
        append=resolved_write_mode == "append",
        write_mode=resolved_write_mode,
        gp_distributed_by_key=normalize_key_columns(
            gp_distributed_by_key,
            "gp_distributed_by_key",
        ),
        gp_partitions=normalized_gp_partitions,
        key_columns=normalize_key_columns(key_columns),
        upsert_partition_column=normalize_upsert_partition_column(upsert_partition_column),
        trino_upsert_partition_drop_sql_template=(
            target_defaults.upsert_partition_drop_sql_template
        ),
        trino_insert_chunk_size=(
            trino_insert_chunk_size
            if trino_insert_chunk_size is not None
            else target_defaults.insert_chunk_size
        ),
        partition_by=normalized_partition_by,
        order_by=adapter.normalize_ch_columns_or_expression(
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
        ch_only_shard=_normalize_only_shard(ch_only_shard),
        ch_retry_per_host_drops=retry_per_host_drops,
        query_label=query_label,
        gp_insert_chunk_size=gp_insert_chunk_size,
        transfer_staging_schema=config.transfer_staging_schema,
        s3_transfer_staging_schema=getattr(
            config,
            "s3_transfer_staging_schema",
            None,
        ),
        s3_transfer_staging_location=target_defaults.s3_transfer_staging_location,
        parquet_storage_options=parquet_storage_options(config),
        transfer_staging_username=_sanitize_transfer_staging_username(config.user),
        use_parquet_staging=(
            adapter.resolve_transfer_staging_mode(
                None,
                s3_transfer_staging_schema=getattr(
                    config, "s3_transfer_staging_schema", None
                ),
                s3_transfer_staging_location=target_defaults.s3_transfer_staging_location,
            )
            == "parquet"
        ),
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        regular_ddl_properties=ddl.regular_properties,
        staging_ddl_properties=ddl.staging_properties,
        parquet_ddl_properties=ddl.parquet_properties,
        regular_ch_policy=ddl.regular_ch_policy,
        staging_ch_policy=ddl.staging_ch_policy,
    )

    if options.write_mode == "upsert" and not options.key_columns:
        raise ValueError("key_columns are required for write_mode='upsert'.")
    if (
        options.write_mode == "upsert"
        and adapter.uses_partition_replacement_upsert()
        and options.upsert_partition_column is None
    ):
        raise ValueError(
            "upsert_partition_column is required for write_mode='upsert' "
            "when db_key has type 'trino' or 'ch'."
        )
    if (
        options.write_mode == "upsert"
        and adapter.needs_upsert_partition_drop_template()
        and not options.trino_upsert_partition_drop_sql_template
    ):
        raise ValueError(
            "Trino write_mode='upsert' requires "
            "upsert_partition_drop_sql_template in the target connection config."
        )
    adapter.validate_gp_distributed_by_key_option(
        options.gp_distributed_by_key,
        option_owner="db_key",
    )
    adapter.validate_gp_insert_chunk_size_option(
        options.gp_insert_chunk_size,
        option_owner="db_key",
    )
    adapter.validate_trino_insert_chunk_size_option(
        options.trino_insert_chunk_size,
        option_owner="db_key",
    )
    adapter.validate_ch_create_table_options(
        option_owner="db_key",
        partition_by=options.partition_by,
        order_by=options.order_by,
        ch_engine=options.ch_engine,
        ch_cluster=options.ch_cluster,
        ch_sharding_key=options.ch_sharding_key,
        ch_only_shard=options.ch_only_shard,
    )
    return options


def _resolve_load_write_mode(
    connection_backend: str,
    *,
    append: bool,
    write_mode: str | None,
) -> str:
    if write_mode is None:
        return "append" if append else "replace"

    normalized = get_backend_adapter(connection_backend).validate_write_mode(write_mode)
    if append and normalized != "append":
        raise ValueError("append=True cannot be combined with write_mode other than 'append'.")
    return normalized


def _normalize_only_shard(ch_only_shard: bool) -> bool:
    if not isinstance(ch_only_shard, bool):
        raise ValueError("ch_only_shard must be a boolean.")
    return ch_only_shard


def _initialize_load_state(options: LoadOptions, connection: Any) -> LoadState:
    target_exists = table_exists(
        options.connection_backend,
        connection,
        options.destination_table,
        connection_key=options.connection_key,
    )
    return LoadState(
        target_exists=target_exists,
        original_target_exists=target_exists,
    )


def _handle_empty_dataframe_load(
    options: LoadOptions,
    state: LoadState,
    *,
    operation_metadata: SqlOperationMetadata,
    return_metadata: bool,
) -> int | SqlOperationResult:
    if options.append and state.target_exists:
        time_print(f"Skipping empty DataFrame append into {options.destination_table}")
        if return_metadata:
            operation_metadata.inserted_rows = 0
            operation_metadata.affected_rows = 0
            return SqlOperationResult(
                rows=0,
                metadata=operation_metadata,
            )
        return 0
    if options.write_mode == "upsert" and state.target_exists:
        time_print(f"Skipping empty DataFrame upsert into {options.destination_table}")
        if return_metadata:
            operation_metadata.inserted_rows = 0
            operation_metadata.affected_rows = 0
            return SqlOperationResult(
                rows=0,
                metadata=operation_metadata,
            )
        return 0
    raise ValueError("Cannot create or replace a table from an empty DataFrame.")


def _validate_load_dataframe(options: LoadOptions, df: pd.DataFrame) -> None:
    if options.table_schema is not None:
        validate_table_schema_columns(options.table_schema, df.columns)

    if options.gp_distributed_by_key:
        validate_key_columns_in_columns(options.gp_distributed_by_key, df.columns)

    validate_key_columns_in_columns(options.key_columns, df.columns)
    validate_upsert_partition_column_in_columns(
        options.upsert_partition_column,
        df.columns,
    )
    get_backend_adapter(options.connection_backend).validate_ch_columns_in_columns(
        options.partition_by,
        df.columns,
        "partition_by",
        data_name="staged data",
    )
    get_backend_adapter(options.connection_backend).validate_ch_columns_in_columns(
        options.order_by,
        df.columns,
        "order_by",
        data_name="staged data",
    )
    _validate_dataframe_key_uniqueness(df, options.key_columns)


def _prepare_load_target(
    *,
    options: LoadOptions,
    state: LoadState,
    connection: Any,
    df: pd.DataFrame,
) -> None:
    _apply_load_target_write_mode(options, state, connection)
    _ensure_load_target_table(options, state, connection, df)
    _load_target_column_metadata(options, state, connection)


def _apply_load_target_write_mode(
    options: LoadOptions,
    state: LoadState,
    connection: Any,
) -> None:
    if options.write_mode in {"append", "upsert"}:
        return

    state.target_exists = apply_target_write_mode(
        options.connection_backend,
        connection,
        options.destination_table,
        write_mode=options.write_mode,
        target_exists=state.target_exists,
        replace_existing_non_ch="drop",
        ch_cluster=options.ch_cluster,
        connection_label=options.connection_key,
        drop_missing_ch_truncate_target=False,
        query_label=options.query_label,
        connection_key=options.connection_key,
        ch_retry_per_host_drops=options.ch_retry_per_host_drops,
        ch_only_shard=options.ch_only_shard,
    )


def _ensure_load_target_table(
    options: LoadOptions,
    state: LoadState,
    connection: Any,
    df: pd.DataFrame,
) -> None:
    adapter = get_backend_adapter(options.connection_backend)
    if not adapter.should_ensure_load_target_table(state.target_exists):
        return

    _create_load_target_table(options, state, connection, df)
    if not state.original_target_exists:
        state.target_created_by_operation = True
    state.target_exists = True


def _create_load_target_table(
    options: LoadOptions,
    state: LoadState,
    connection: Any,
    df: pd.DataFrame,
) -> None:
    create_kwargs: dict[str, Any] = {}
    if options.query_label is not None:
        create_kwargs["query_label"] = options.query_label
    if options.table_schema is not None:
        create_kwargs["table_schema"] = options.table_schema
    create_kwargs.update(
        get_backend_adapter(options.connection_backend).build_load_target_create_kwargs(
            gp_distributed_by_key=options.gp_distributed_by_key,
            gp_partitions=options.gp_partitions,
            partition_by=options.partition_by,
            order_by=options.order_by,
            ch_engine=options.ch_engine,
            ch_cluster=options.ch_cluster,
            ch_sharding_key=options.ch_sharding_key,
            ch_only_shard=options.ch_only_shard,
            write_mode=options.write_mode,
            original_target_exists=state.original_target_exists,
        )
    )
    if options.regular_ddl_properties:
        create_kwargs["ddl_properties"] = options.regular_ddl_properties
    if options.regular_ch_policy is not None:
        create_kwargs["ch_creation_policy"] = options.regular_ch_policy

    _create_sql_table_with_connection(
        options.connection_backend,
        connection,
        options.destination_table,
        None if options.table_schema is not None else df,
        connection_key=options.connection_key,
        **create_kwargs,
    )


def _load_target_column_metadata(
    options: LoadOptions,
    state: LoadState,
    connection: Any,
) -> None:
    if get_backend_adapter(
        options.connection_backend,
    ).requires_load_target_column_metadata(
        write_mode=options.write_mode,
        original_target_exists=state.original_target_exists,
    ):
        state.target_column_types = get_table_column_types(
            options.connection_backend,
            connection,
            options.destination_table,
            connection_key=options.connection_key,
        )


def _analyze_load_target(options: LoadOptions, connection: Any) -> None:
    if options.query_label is None:
        analyze_table(
            connection_type=options.connection_backend,
            connection=connection,
            table_name=options.destination_table,
        )
        return

    analyze_table(
        connection_type=options.connection_backend,
        connection=connection,
        table_name=options.destination_table,
        query_label=options.query_label,
    )


def _build_load_result(
    *,
    options: LoadOptions,
    state: LoadState,
    source_rows: int,
    inserted_rows: int,
    operation_metadata: SqlOperationMetadata,
    return_metadata: bool,
) -> int | SqlOperationResult:
    if not return_metadata:
        return inserted_rows

    return SqlOperationResult(
        rows=inserted_rows,
        metadata=_build_load_metadata(
            options=options,
            state=state,
            source_rows=source_rows,
            inserted_rows=inserted_rows,
            operation_metadata=operation_metadata,
        ),
    )


def build_load_df_plan(options: LoadOptions, df: pd.DataFrame) -> SqlPlan:
    adapter = get_backend_adapter(options.connection_backend)
    uses_partition_replacement_upsert = adapter.uses_partition_replacement_upsert()
    metadata = SqlOperationMetadata(
        source_rows=len(df),
        staged_rows=(
            len(df)
            if options.use_parquet_staging or (options.append and options.key_columns)
            else None
        ),
        inserted_rows=len(df),
        affected_rows=len(df),
    )
    plan = SqlPlan(
        operation="load_df",
        target_alias=options.connection_key,
        target_backend=options.connection_backend,
        target_table=options.destination_table,
        options={
            "write_mode": options.write_mode,
            "append": options.append,
            "key_columns": options.key_columns,
            "upsert_partition_column": options.upsert_partition_column,
            "gp_distributed_by_key": options.gp_distributed_by_key,
            "gp_partitions": _gp_partition_plan_option(options.gp_partitions),
            "trino_insert_chunk_size": options.trino_insert_chunk_size,
            "gp_insert_chunk_size": options.gp_insert_chunk_size,
            "table_schema": options.table_schema,
            "partition_by": options.partition_by,
            "order_by": options.order_by,
            "ch_engine": options.ch_engine,
            "ch_cluster": options.ch_cluster,
            "ch_sharding_key": options.ch_sharding_key,
            "ch_only_shard": options.ch_only_shard,
            "ch_ddl_wait_policy": (
                options.regular_ch_policy.ddl_wait_policy
                if options.regular_ch_policy is not None
                else None
            ),
            "transfer_staging_schema": options.transfer_staging_schema,
            "s3_transfer_staging_schema": (options.s3_transfer_staging_schema),
            "s3_transfer_staging_location": options.s3_transfer_staging_location,
            "use_parquet_staging": options.use_parquet_staging,
        },
        metadata=metadata,
    )

    if df.empty:
        return plan

    if options.write_mode == "replace":
        add_drop_target_steps(
            plan,
            alias=options.connection_key,
            backend=options.connection_backend,
            table_name=options.destination_table,
            ch_cluster=options.ch_cluster,
            query_label=options.query_label,
            ch_only_shard=options.ch_only_shard,
        )
    elif options.write_mode == "truncate_insert":
        add_clear_target_steps(
            plan,
            alias=options.connection_key,
            backend=options.connection_backend,
            table_name=options.destination_table,
            query_label=options.query_label,
            include_ch_shard=(
                adapter.supports_distributed_table_targets() and not options.ch_only_shard
            ),
            ch_cluster=options.ch_cluster,
            ch_only_shard=options.ch_only_shard,
        )

    if options.write_mode in {
        "replace",
        "truncate_insert",
    } or adapter.should_ensure_load_target_table(target_exists=True):
        create_kwargs = adapter.build_load_target_create_kwargs(
            gp_distributed_by_key=options.gp_distributed_by_key,
            gp_partitions=options.gp_partitions,
            partition_by=options.partition_by,
            order_by=options.order_by,
            ch_engine=options.ch_engine,
            ch_cluster=options.ch_cluster,
            ch_sharding_key=options.ch_sharding_key,
            ch_only_shard=options.ch_only_shard,
            write_mode=options.write_mode,
            original_target_exists=options.write_mode == "replace",
        )
        add_create_table_steps(
            plan,
            _build_create_table_sqls(
                options.connection_backend,
                options.destination_table,
                df,
                table_schema=options.table_schema,
                query_label=options.query_label,
                ddl_properties=options.regular_ddl_properties,
                ch_creation_policy=options.regular_ch_policy,
                **create_kwargs,
            ),
            alias=options.connection_key,
            backend=options.connection_backend,
            table_name=options.destination_table,
        )

    if options.use_parquet_staging:
        _add_parquet_load_plan_steps(plan, options, df, metadata)
    elif options.write_mode == "upsert":
        stage_table = _dry_run_load_stage_name(options, "dry_run")
        final_stage_table = _dry_run_load_stage_name(options, "upsert_final__dry_run")
        metadata.stage_table = stage_table
        add_create_table_steps(
            plan,
            _build_create_table_sqls(
                options.connection_backend,
                stage_table,
                df,
                table_schema=options.table_schema,
                gp_distributed_by_key=options.gp_distributed_by_key,
                query_label=options.query_label,
                ddl_properties=options.staging_ddl_properties,
                ch_creation_policy=options.staging_ch_policy,
            ),
            alias=options.connection_key,
            backend=options.connection_backend,
            phase="create_stage",
            table_name=stage_table,
        )
        add_load_stage_step(
            plan,
            alias=options.connection_key,
            backend=options.connection_backend,
            stage_table=stage_table,
            sql=_build_dataframe_insert_placeholder(
                options.connection_backend,
                stage_table,
                df,
            ),
            query_label=options.query_label,
        )
        if uses_partition_replacement_upsert:
            add_create_table_steps(
                plan,
                _build_create_table_sqls(
                    options.connection_backend,
                    final_stage_table,
                    df,
                    table_schema=options.table_schema,
                    gp_distributed_by_key=options.gp_distributed_by_key,
                    query_label=options.query_label,
                    ddl_properties=options.staging_ddl_properties,
                    ch_creation_policy=options.staging_ch_policy,
                ),
                alias=options.connection_key,
                backend=options.connection_backend,
                phase="create_final_upsert_stage",
                table_name=final_stage_table,
            )
        plan.extend(
            build_upsert_stage_sqls(
                options.connection_backend,
                options.destination_table,
                stage_table,
                columns=[str(column) for column in df.columns],
                key_columns=options.key_columns or [],
                column_types=options.table_schema,
                ch_cluster=options.ch_cluster,
                ch_only_shard=options.ch_only_shard,
                query_label=options.query_label,
                upsert_partition_column=options.upsert_partition_column,
                final_stage_table=(
                    final_stage_table if uses_partition_replacement_upsert else None
                ),
                trino_partition_drop_sql_template=(
                    options.trino_upsert_partition_drop_sql_template
                ),
            ),
            alias=options.connection_key,
            backend=options.connection_backend,
            phase="upsert_target",
            target_table=options.destination_table,
        )
        add_cleanup_stage_step(
            plan,
            alias=options.connection_key,
            backend=options.connection_backend,
            stage_table=stage_table,
            query_label=options.query_label,
        )
        if uses_partition_replacement_upsert:
            add_cleanup_stage_step(
                plan,
                alias=options.connection_key,
                backend=options.connection_backend,
                stage_table=final_stage_table,
                query_label=options.query_label,
            )
    elif options.append and options.key_columns:
        stage_table = _dry_run_load_stage_name(options, "dry_run")
        metadata.stage_table = stage_table
        add_create_table_steps(
            plan,
            _build_create_table_sqls(
                options.connection_backend,
                stage_table,
                df,
                table_schema=options.table_schema,
                gp_distributed_by_key=options.gp_distributed_by_key,
                query_label=options.query_label,
                ddl_properties=options.staging_ddl_properties,
                ch_creation_policy=options.staging_ch_policy,
            ),
            alias=options.connection_key,
            backend=options.connection_backend,
            phase="create_stage",
            table_name=stage_table,
        )
        add_load_stage_step(
            plan,
            alias=options.connection_key,
            backend=options.connection_backend,
            stage_table=stage_table,
            sql=_build_dataframe_insert_placeholder(
                options.connection_backend,
                stage_table,
                df,
            ),
            query_label=options.query_label,
        )
        add_insert_from_stage_step(
            plan,
            alias=options.connection_key,
            backend=options.connection_backend,
            target_table=options.destination_table,
            stage_table=stage_table,
            phase="insert_from_stage",
            query_label=options.query_label,
        )
        add_cleanup_stage_step(
            plan,
            alias=options.connection_key,
            backend=options.connection_backend,
            stage_table=stage_table,
            query_label=options.query_label,
        )
    else:
        plan.add(
            _build_dataframe_insert_placeholder(
                options.connection_backend,
                options.destination_table,
                df,
            ),
            alias=options.connection_key,
            backend=options.connection_backend,
            phase="load_data",
            target_table=options.destination_table,
            query_label=options.query_label,
        )

    add_analyze_step(
        plan,
        alias=options.connection_key,
        backend=options.connection_backend,
        table_name=options.destination_table,
        query_label=options.query_label,
    )
    add_count_step(
        plan,
        alias=options.connection_key,
        backend=options.connection_backend,
        table_name=options.destination_table,
        query_label=options.query_label,
    )
    return plan


def _load_df_preview_sql(options: LoadOptions, df: pd.DataFrame) -> str | None:
    plan = build_load_df_plan(options, df)
    if not plan.sqls:
        return None
    return plan.sqls[0]


def _build_dataframe_insert_placeholder(
    connection_backend: str,
    table_name: str,
    df: pd.DataFrame,
) -> str:
    columns = column_list_sql([str(column) for column in df.columns], connection_backend)
    row_word = "row" if len(df) == 1 else "rows"
    return f"INSERT INTO {table_name} ({columns}) VALUES <{len(df)} dataframe {row_word}>"


def _add_parquet_load_plan_steps(
    plan: SqlPlan,
    options: LoadOptions,
    df: pd.DataFrame,
    metadata: SqlOperationMetadata,
) -> None:
    adapter = get_backend_adapter(options.connection_backend)
    uses_partition_replacement_upsert = adapter.uses_partition_replacement_upsert()
    parquet_schema = options.s3_transfer_staging_schema
    stage_table = build_stage_table_name(
        options.connection_backend,
        options.destination_table,
        transfer_staging_schema=parquet_schema,
        transfer_staging_username=options.transfer_staging_username,
        random_suffix="dryrun",
        destination_hash=options.destination_hash,
    )
    stage_external_location = build_stage_external_location(
        options,
        stage_suffix="dryrun",
    )
    metadata.stage_table = stage_table
    metadata.stage_external_location = stage_external_location

    add_create_table_steps(
        plan,
        [
            build_create_parquet_stage_table_sql(
                stage_table,
                options.table_schema,
                stage_external_location,
                query_label=options.query_label,
            )
        ],
        alias=options.connection_key,
        backend=options.connection_backend,
        phase="create_stage",
        table_name=stage_table,
    )
    add_load_stage_step(
        plan,
        alias=options.connection_key,
        backend=options.connection_backend,
        stage_table=stage_table,
        sql=(
            "WRITE PARQUET FILES TO "
            f"{stage_external_location} FROM <{len(df)} dataframe "
            f"{'row' if len(df) == 1 else 'rows'}>"
        ),
        query_label=options.query_label,
    )

    if options.write_mode == "upsert":
        final_stage_table = _dry_run_load_stage_name(options, "upsert_final__dry_run")
        if uses_partition_replacement_upsert:
            add_create_table_steps(
                plan,
                _build_create_table_sqls(
                    options.connection_backend,
                    final_stage_table,
                    df,
                    table_schema=options.table_schema,
                    query_label=options.query_label,
                    ddl_properties=options.staging_ddl_properties,
                    ch_creation_policy=options.staging_ch_policy,
                ),
                alias=options.connection_key,
                backend=options.connection_backend,
                phase="create_final_upsert_stage",
                table_name=final_stage_table,
            )
        plan.extend(
            build_upsert_stage_sqls(
                options.connection_backend,
                options.destination_table,
                stage_table,
                columns=[str(column) for column in df.columns],
                key_columns=options.key_columns or [],
                column_types=options.table_schema,
                query_label=options.query_label,
                upsert_partition_column=options.upsert_partition_column,
                final_stage_table=(
                    final_stage_table if uses_partition_replacement_upsert else None
                ),
                trino_partition_drop_sql_template=(
                    options.trino_upsert_partition_drop_sql_template
                ),
            ),
            alias=options.connection_key,
            backend=options.connection_backend,
            phase="upsert_target",
            target_table=options.destination_table,
        )
    else:
        add_insert_from_stage_step(
            plan,
            alias=options.connection_key,
            backend=options.connection_backend,
            target_table=options.destination_table,
            stage_table=stage_table,
            phase="insert_from_stage",
            query_label=options.query_label,
        )

    add_cleanup_stage_step(
        plan,
        alias=options.connection_key,
        backend=options.connection_backend,
        stage_table=stage_table,
        query_label=options.query_label,
    )
    if options.write_mode == "upsert" and uses_partition_replacement_upsert:
        add_cleanup_stage_step(
            plan,
            alias=options.connection_key,
            backend=options.connection_backend,
            stage_table=_dry_run_load_stage_name(options, "upsert_final__dry_run"),
            query_label=options.query_label,
        )
    plan.add(
        f"DELETE STAGE FILES {stage_external_location}",
        alias=options.connection_key,
        backend=options.connection_backend,
        phase="cleanup_stage_location",
        target_table=stage_table,
        query_label=options.query_label,
    )


def _dry_run_load_stage_name(options: LoadOptions, suffix: str) -> str:
    return build_stage_table_name(
        options.connection_backend,
        options.destination_table,
        transfer_staging_schema=(
            options.s3_transfer_staging_schema
        ),
        transfer_staging_username=options.transfer_staging_username,
        random_suffix=suffix,
        destination_hash=options.destination_hash,
    )


def _build_load_metadata(
    *,
    options: LoadOptions,
    state: LoadState,
    source_rows: int,
    inserted_rows: int,
    operation_metadata: SqlOperationMetadata,
) -> SqlOperationMetadata:
    metadata = operation_metadata
    metadata.source_rows = source_rows
    metadata.staged_rows = source_rows if state.overlap_stage_table is not None else None
    metadata.inserted_rows = inserted_rows
    metadata.affected_rows = inserted_rows
    metadata.stage_table = state.overlap_stage_table
    metadata.stage_external_location = state.stage_external_location
    try:
        metadata.final_target_rows = _run_load_target_action(
            options,
            "target_metadata",
            lambda connection_ref: count_table_rows(
                options.connection_backend,
                connection_ref["connection"],
                options.destination_table,
                query_label=options.query_label,
            ),
        )
    except Exception:
        metadata.final_target_rows = None
    return metadata


def _load_dataframe(
    options: LoadOptions,
    state: LoadState,
    df: pd.DataFrame,
    on_progress: Any | None = None,
) -> int:
    if options.use_parquet_staging:
        return _load_dataframe_via_parquet_stage(
            options=options,
            state=state,
            df=df,
            on_progress=on_progress,
        )

    if (options.append and state.target_exists and options.key_columns) or (
        options.write_mode == "upsert" and state.original_target_exists
    ):
        stage_create_kwargs: dict[str, Any] = {}
        if options.table_schema is not None:
            stage_create_kwargs["table_schema"] = options.table_schema
        state.overlap_stage_table = _run_load_target_action(
            options,
            "create_stage",
            lambda connection_ref: create_stage_table(
                connection_type=options.connection_backend,
                connection=connection_ref["connection"],
                target_table=options.destination_table,
                batch=df,
                gp_distributed_by_key=options.gp_distributed_by_key,
                connection_key=options.connection_key,
                query_label=options.query_label,
                transfer_staging_schema=options.transfer_staging_schema,
                transfer_staging_username=options.transfer_staging_username,
                destination_hash=options.destination_hash,
                **stage_create_kwargs,
            ),
        )
        _run_load_target_action(
            options,
            "insert_stage",
            lambda connection_ref: insert_table_batch(
                options.connection_backend,
                connection_ref,
                state.overlap_stage_table,
                df,
                retry_fn=run_with_retry,
                retry_cnt=1,
                timeout_increment=0,
                target_column_types=options.table_schema or state.target_column_types,
                trino_insert_chunk_size=options.trino_insert_chunk_size,
                gp_insert_chunk_size=options.gp_insert_chunk_size,
                query_label=options.query_label,
                on_progress=on_progress,
                connection_key=options.connection_key,
                rollback_fn=rollback_quietly,
                replace_connection_fn=replace_connection,
            ),
        )
        if options.write_mode == "upsert":
            _run_load_target_action(
                options,
                "validate_stage",
                lambda connection_ref: validate_stage_uniqueness(
                    connection_type=options.connection_backend,
                    connection=connection_ref["connection"],
                    stage_table=state.overlap_stage_table,
                    key_columns=options.key_columns,
                ),
            )
            _ensure_final_upsert_stage_table(options, state, df)
            try:
                _run_load_target_action(
                    options,
                    "finalize_target",
                    lambda connection_ref: upsert_stage_table(
                        options.connection_backend,
                        connection_ref["connection"],
                        options.destination_table,
                        state.overlap_stage_table,
                        columns=[str(column) for column in df.columns],
                        key_columns=options.key_columns or [],
                        column_types=options.table_schema or state.target_column_types,
                        ch_cluster=options.ch_cluster,
                        ch_only_shard=options.ch_only_shard,
                        query_label=options.query_label,
                        upsert_partition_column=options.upsert_partition_column,
                        final_stage_table=state.final_upsert_stage_table,
                        trino_partition_drop_sql_template=(
                            options.trino_upsert_partition_drop_sql_template
                        ),
                    ),
                )
            except Exception as exc:
                get_backend_adapter(
                    options.connection_backend,
                ).mark_upsert_finalization_error(exc)
                raise
            return len(df)

        _run_load_target_action(
            options,
            "validate_stage",
            lambda connection_ref: validate_stage_target_key_overlap(
                connection_type=options.connection_backend,
                connection=connection_ref["connection"],
                stage_table=state.overlap_stage_table,
                target_table=options.destination_table,
                key_columns=options.key_columns,
                target_exists=state.target_exists,
                replace_target_table=False,
            ),
        )
        _run_load_target_action(
            options,
            "finalize_target",
            lambda connection_ref: insert_from_table(
                options.connection_backend,
                connection_ref["connection"],
                options.destination_table,
                state.overlap_stage_table,
                query_label=options.query_label,
            ),
        )
        return len(df)

    return _run_load_target_action(
        options,
        "insert_target",
        lambda connection_ref: insert_table_batch(
            options.connection_backend,
            connection_ref,
            options.destination_table,
            df,
            retry_fn=run_with_retry,
            retry_cnt=1,
            timeout_increment=0,
            target_column_types=state.target_column_types,
            trino_insert_chunk_size=options.trino_insert_chunk_size,
            gp_insert_chunk_size=options.gp_insert_chunk_size,
            query_label=options.query_label,
            on_progress=on_progress,
            connection_key=options.connection_key,
            rollback_fn=rollback_quietly,
            replace_connection_fn=replace_connection,
        ),
    )


def _load_dataframe_via_parquet_stage(
    *,
    options: LoadOptions,
    state: LoadState,
    df: pd.DataFrame,
    on_progress: Any | None,
) -> int:
    pa, pq, fsspec_module = ensure_parquet_staging_dependencies()
    _run_load_target_action(
        options,
        "create_stage",
        lambda connection_ref: _create_load_parquet_stage_table(
            options,
            state,
            connection_ref["connection"],
        ),
    )
    if state.stage_external_location is None or state.overlap_stage_table is None:
        raise RuntimeError("Parquet load stage was not initialized.")

    inserted_rows = write_dataframe_to_parquet_stage(
        df,
        stage_external_location=state.stage_external_location,
        pa=pa,
        pq=pq,
        fsspec_module=fsspec_module,
        storage_options=options.parquet_storage_options,
        row_group_size=PARQUET_STAGE_DEFAULT_MAX_ROW_GROUP_SIZE,
        on_progress=on_progress,
    )
    _run_load_target_action(
        options,
        "finalize_target",
        lambda connection_ref: _finalize_loaded_dataframe_stage(
            options=options,
            state=state,
            connection=connection_ref["connection"],
            df=df,
        ),
    )
    return inserted_rows


def _create_load_parquet_stage_table(
    options: LoadOptions,
    state: LoadState,
    connection: Any,
) -> None:
    parquet_schema = options.s3_transfer_staging_schema
    if not parquet_schema:
        raise ValueError("s3_transfer_staging_schema is required for Parquet staging.")
    if not options.s3_transfer_staging_location:
        raise ValueError("s3_transfer_staging_location is required for Parquet staging.")
    stage_column_types = options.table_schema or state.target_column_types
    if stage_column_types is None:
        raise ValueError(
            "Could not resolve target schema before creating a Parquet load stage "
            "table. Pass table_schema or create/load a target table with inspectable "
            "column types."
        )

    for attempt in range(1, STAGE_TABLE_NAME_MAX_ATTEMPTS + 1):
        stage_table = build_stage_table_name(
            options.connection_backend,
            options.destination_table,
            transfer_staging_schema=parquet_schema,
            transfer_staging_username=options.transfer_staging_username,
            destination_hash=options.destination_hash,
        )
        if table_exists(
            options.connection_backend,
            connection,
            stage_table,
            connection_key=options.connection_key,
        ):
            time_print(
                f"Stage table name collision detected for {stage_table}; "
                f"retrying with a new name "
                f"({attempt}/{STAGE_TABLE_NAME_MAX_ATTEMPTS})"
            )
            continue

        stage_external_location = build_stage_external_location(options)
        create_sql = build_create_parquet_stage_table_sql(
            stage_table,
            stage_column_types,
            stage_external_location,
            query_label=options.query_label,
        )
        get_backend_adapter(options.connection_backend).execute_command(
            connection,
            create_sql,
        )
        state.overlap_stage_table = stage_table
        state.stage_external_location = stage_external_location
        return

    raise RuntimeError("Could not generate a unique Parquet load stage table name.")


def _finalize_loaded_dataframe_stage(
    *,
    options: LoadOptions,
    state: LoadState,
    connection: Any,
    df: pd.DataFrame,
) -> None:
    if state.overlap_stage_table is None:
        raise RuntimeError("Parquet load stage table was not initialized.")

    if options.write_mode == "upsert":
        validate_stage_uniqueness(
            connection_type=options.connection_backend,
            connection=connection,
            stage_table=state.overlap_stage_table,
            key_columns=options.key_columns,
        )
        if state.original_target_exists:
            _ensure_final_upsert_stage_table(options, state, df)
            upsert_stage_table(
                options.connection_backend,
                connection,
                options.destination_table,
                state.overlap_stage_table,
                columns=[str(column) for column in df.columns],
                key_columns=options.key_columns or [],
                column_types=options.table_schema or state.target_column_types,
                query_label=options.query_label,
                upsert_partition_column=options.upsert_partition_column,
                final_stage_table=state.final_upsert_stage_table,
                trino_partition_drop_sql_template=(
                    options.trino_upsert_partition_drop_sql_template
                ),
            )
            return

    if options.append and state.target_exists and options.key_columns:
        validate_stage_target_key_overlap(
            connection_type=options.connection_backend,
            connection=connection,
            stage_table=state.overlap_stage_table,
            target_table=options.destination_table,
            key_columns=options.key_columns,
            target_exists=state.target_exists,
            replace_target_table=False,
        )

    insert_from_table(
        options.connection_backend,
        connection,
        options.destination_table,
        state.overlap_stage_table,
        query_label=options.query_label,
    )


def _ensure_final_upsert_stage_table(
    options: LoadOptions,
    state: LoadState,
    df: pd.DataFrame,
) -> None:
    if not get_backend_adapter(
        options.connection_backend,
    ).uses_partition_replacement_upsert():
        return
    if not state.original_target_exists:
        return
    if state.final_upsert_stage_table is not None:
        return

    create_schema = options.table_schema or state.target_column_types
    state.final_upsert_stage_table = _run_load_target_action(
        options,
        "create_final_upsert_stage",
        lambda connection_ref: create_stage_table(
            connection_type=options.connection_backend,
            connection=connection_ref["connection"],
            target_table=options.destination_table,
            batch=df,
            column_types=create_schema,
            gp_distributed_by_key=options.gp_distributed_by_key,
            connection_key=options.connection_key,
            query_label=options.query_label,
            transfer_staging_schema=options.transfer_staging_schema,
            transfer_staging_username=options.transfer_staging_username,
            destination_hash=options.destination_hash,
        ),
    )


class _ProgressTracker:
    def __init__(self, progress_bar: Any) -> None:
        self.progress_bar = progress_bar
        self.rows = 0

    def update(self, rows: int) -> None:
        self.rows += rows
        self.progress_bar.update(rows)

    def complete_to(self, rows: int) -> None:
        remaining_rows = rows - self.rows
        if remaining_rows > 0:
            self.update(remaining_rows)


def _make_load_progress_bar(
    *,
    total: int,
    options: LoadOptions,
    progress: bool,
) -> Any:
    return tqdm(
        total=total,
        desc=f"load_df {options.connection_key}.{options.destination_table}",
        unit="row",
        disable=not progress,
    )


def _validate_progress(progress: bool) -> None:
    validate_progress_option(progress)


def _cleanup_load(
    options: LoadOptions,
    state: LoadState | None,
    *,
    drop_created_target: bool = False,
) -> None:
    if state is not None and state.overlap_stage_table is not None:
        try:
            _run_load_target_action(
                options,
                "cleanup_stage",
                lambda connection_ref: cleanup_stage_table_with_retry(
                    options.connection_backend,
                    options.connection_key,
                    connection_ref,
                    state.overlap_stage_table,
                    retry_fn=run_with_retry,
                    retry_cnt=options.retry_cnt,
                    timeout_increment=options.timeout_increment,
                    rollback_fn=rollback_quietly,
                    replace_connection_fn=replace_connection,
                    query_label=options.query_label,
                ),
            )
        except Exception:
            time_print(f"Failed to drop temporary load_df stage table {state.overlap_stage_table}")
    if state is not None and state.final_upsert_stage_table is not None:
        try:
            _run_load_target_action(
                options,
                "cleanup_final_upsert_stage",
                lambda connection_ref: cleanup_stage_table_with_retry(
                    options.connection_backend,
                    options.connection_key,
                    connection_ref,
                    state.final_upsert_stage_table,
                    retry_fn=run_with_retry,
                    retry_cnt=options.retry_cnt,
                    timeout_increment=options.timeout_increment,
                    rollback_fn=rollback_quietly,
                    replace_connection_fn=replace_connection,
                    query_label=options.query_label,
                ),
            )
        except Exception:
            time_print(
                "Failed to drop temporary load_df final upsert stage table "
                f"{state.final_upsert_stage_table}"
            )
    if drop_created_target and _should_drop_created_load_target(state):
        try:
            _run_load_target_action(
                options,
                "cleanup_target",
                lambda connection_ref: drop_table_with_retry(
                    options.connection_backend,
                    options.connection_key,
                    connection_ref,
                    options.destination_table,
                    retry_fn=run_with_retry,
                    retry_cnt=options.retry_cnt,
                    timeout_increment=options.timeout_increment,
                    rollback_fn=rollback_quietly,
                    replace_connection_fn=replace_connection,
                    query_label=options.query_label,
                    operation_label="created target table",
                ),
            )
        except Exception:
            time_print(
                f"Failed to drop load_df target table {options.destination_table} "
                "created by this failed operation"
            )
    if state is not None and state.stage_external_location is not None:
        try:
            if options.parquet_storage_options is None:
                cleanup_parquet_stage_location(state.stage_external_location)
            else:
                cleanup_parquet_stage_location(
                    state.stage_external_location,
                    storage_options=options.parquet_storage_options,
                )
        except Exception:
            time_print(
                "Failed to delete temporary load_df Parquet stage files "
                f"{state.stage_external_location}"
            )


def _should_drop_created_load_target(state: LoadState | None) -> bool:
    return (
        state is not None and state.target_created_by_operation and not state.original_target_exists
    )


def _run_load_target_action(
    options: LoadOptions,
    role: str,
    operation: Any,
) -> Any:
    return run_with_fresh_connection(
        options.connection_key,
        role,
        operation,
        open_connection=get_sql_connection,
    )


def _validate_dataframe_key_uniqueness(
    df: pd.DataFrame,
    key_columns: list[str] | None,
) -> None:
    if not key_columns:
        return

    null_columns = [column for column in key_columns if df[column].isna().any()]
    if null_columns:
        raise ValueError(
            "Null key values found in DataFrame for key_columns: " + ", ".join(null_columns)
        )

    if df.duplicated(subset=key_columns, keep=False).any():
        raise ValueError(
            "Duplicate key values found in DataFrame for key_columns: " + ", ".join(key_columns)
        )
