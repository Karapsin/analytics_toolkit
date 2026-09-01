from __future__ import annotations

# ruff: noqa: EM101, TRY003

from collections.abc import Mapping, Sequence
import warnings
from typing import Any, cast

import pandas as pd

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.backends import get_backend, get_backend_adapter
from analytics_toolkit.sql.backends.ch.creation_policy import build_policy_create_sqls
from analytics_toolkit.sql.connection.errors import (
    InvalidSqlInputError,
    SqlOperationContext,
    SqlTableReadinessError,
    sql_preview,
)

from ..connection.config import get_connection_config, resolve_connection_backend
from ..connection.get_sql_connection import get_sql_connection
from ..execution.operation_runner import (
    run_connection_operation,
    timed_public_sql_function,
    tracked_sql_operation,
    validate_retry_options,
)
from ..execution.plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
from .builders import (
    _apply_query_label_to_sqls,
    _build_backend_create_table_sqls,
    _validate_only_shard,
)
from .ch_policy import regular_ddl_properties, resolve_create_ch_policy
from .models import CreateSqlTableOptions
from .properties import overlay_with_properties
from .schema import _build_column_definitions, normalize_table_schema
from .target_replace import build_drop_target_sqls, drop_existing_target


@timed_public_sql_function
def create_table(
    db_key: str,
    table_name: str,
    df: pd.DataFrame | None = None,
    *,
    sql: str | None = None,
    source_db: str | None = None,
    insert_data: bool = False,
    drop_if_exists: bool = False,
    if_not_exists: bool = False,
    gp_distributed_by_key: str | Sequence[str] | None = None,
    gp_partitions: Mapping[str, Any] | None = None,
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
    ch_replace_table: bool = False,
    retry_cnt: int = 5,
    timeout_increment: float = 5,
    dry_run: bool = False,
    return_sql: bool = False,
    only_generate_sql: bool = False,
    query_label: str | None = None,
    return_metadata: bool = False,
    table_schema: Mapping[str, str] | None = None,
    drop_target_if_exists: bool | None = None,
) -> str | SqlPlan | SqlOperationResult | int | None:
    validate_retry_options(retry_cnt, timeout_increment)
    drop_if_exists = _resolve_drop_if_exists(
        drop_if_exists=drop_if_exists,
        drop_target_if_exists=drop_target_if_exists,
    )
    _validate_create_mode(
        drop_if_exists=drop_if_exists,
        if_not_exists=if_not_exists,
        ch_replace_table=ch_replace_table,
    )
    _validate_create_schema_sources(
        df=df,
        sql=sql,
        table_schema=table_schema,
    )
    config = get_connection_config(db_key)
    if not (dry_run or return_sql or only_generate_sql):
        existing_result = _handle_existing_create_target(
            config=config,
            table_name=table_name,
            drop_if_exists=drop_if_exists,
            if_not_exists=if_not_exists,
            ch_replace_table=ch_replace_table,
            query_label=query_label,
            return_metadata=return_metadata,
        )
        if existing_result is not _CREATE_CONTINUE:
            return cast("str | SqlPlan | SqlOperationResult | int | None", existing_result)
    if sql is not None:
        return _create_sql_table_from_sql_source(
            db_key=db_key,
            table_name=table_name,
            sql=sql,
            source_db=source_db,
            insert_data=insert_data,
            drop_target_if_exists=drop_if_exists,
            gp_distributed_by_key=gp_distributed_by_key,
            gp_partitions=gp_partitions,
            partition_by=partition_by,
            order_by=order_by,
            ch_policy_overrides={
                "ch_engine": ch_engine,
                "ch_cluster": ch_cluster,
                "ch_sharding_key": ch_sharding_key,
                "ch_distributed_table": ch_distributed_table,
                "ch_distributed_engine_template": ch_distributed_engine_template,
                "ch_distributed_cluster": ch_distributed_cluster,
                "ch_shard_on_cluster": ch_shard_on_cluster,
                "ch_distributed_on_cluster": ch_distributed_on_cluster,
            },
            ch_ddl_ready_timeout_seconds=ch_ddl_ready_timeout_seconds,
            ch_ddl_wait_policy=ch_ddl_wait_policy,
            ch_only_shard=ch_only_shard,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            dry_run=dry_run,
            return_sql=return_sql,
            only_generate_sql=only_generate_sql,
            query_label=query_label,
            return_metadata=return_metadata,
        )

    options = _build_create_table_options(
        connection_key=config.connection_key,
        backend=config.backend,
        table_name=table_name,
        df=df,
        table_schema=table_schema,
        gp_distributed_by_key=gp_distributed_by_key,
        gp_partitions=gp_partitions,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=ch_distributed_table,
        ch_only_shard=ch_only_shard,
        ch_replace_table=ch_replace_table,
        drop_target_if_exists=drop_if_exists,
        if_not_exists=if_not_exists,
        dry_run=dry_run,
        return_sql=return_sql,
        query_label=query_label,
        return_metadata=return_metadata,
        ddl_properties=regular_ddl_properties(config),
        ch_creation_policy=resolve_create_ch_policy(
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
        ),
        option_owner="db_key",
    )
    create_sqls = _build_create_sql_table_sqls(options, option_owner="db_key")
    drop_sqls = build_drop_target_sqls(options)
    metadata, plan = _build_create_table_plan(options, drop_sqls, create_sqls)

    if only_generate_sql:
        return _format_sql_statements([*drop_sqls, *create_sqls])
    if options.dry_run or options.return_sql:
        return plan

    def operation(connection_ref: dict[str, Any], attempt: int) -> None:
        drop_existing_target(
            options=options,
            connection=connection_ref["connection"],
            drop_sqls=drop_sqls,
            metadata=metadata,
            retry_attempt=attempt,
        )
        _execute_create_sql_table(
            options=options,
            connection=connection_ref["connection"],
            create_sqls=create_sqls,
            metadata=metadata,
            retry_attempt=attempt,
        )

    def context(attempt: int) -> SqlOperationContext:
        return SqlOperationContext(
            operation="create_table",
            alias=options.connection_key,
            backend=options.backend,
            phase=("replace_target" if options.drop_target_if_exists else "create_target"),
            target_table=options.table_name,
            retry_attempt=attempt,
            sql_preview=sql_preview((drop_sqls or create_sqls or [options.table_name])[0]),
        )

    run_connection_operation(
        operation_name=(
            f"creating table {options.connection_key}.{options.table_name} ({options.backend})"
        ),
        connection_key=options.connection_key,
        backend=options.backend,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        open_connection=get_sql_connection,
        operation=operation,
        context_factory=context,
    )
    if options.return_metadata:
        return SqlOperationResult(rows=None, metadata=metadata, plan=plan)
    return None


def _validate_create_schema_sources(
    *,
    df: pd.DataFrame | None,
    sql: str | None,
    table_schema: Mapping[str, str] | None,
) -> None:
    provided = [
        source
        for source, value in (
            ("df", df),
            ("sql", sql),
            ("table_schema", table_schema),
        )
        if value is not None
    ]
    if len(provided) != 1:
        raise InvalidSqlInputError(
            "Exactly one schema source must be provided: df, sql, or table_schema."
        )


_CREATE_CONTINUE = object()


def _resolve_drop_if_exists(
    *,
    drop_if_exists: bool,
    drop_target_if_exists: bool | None,
) -> bool:
    if not isinstance(drop_if_exists, bool):
        raise TypeError("drop_if_exists must be a boolean.")
    if drop_target_if_exists is None:
        return drop_if_exists
    warnings.warn(
        "drop_target_if_exists is deprecated; use drop_if_exists instead.",
        DeprecationWarning,
        stacklevel=3,
    )
    if not isinstance(drop_target_if_exists, bool):
        raise TypeError("drop_target_if_exists must be a boolean.")
    if drop_if_exists:
        raise InvalidSqlInputError(
            "Use only drop_if_exists; do not also pass drop_target_if_exists."
        )
    return drop_target_if_exists


def _validate_create_mode(
    *,
    drop_if_exists: bool,
    if_not_exists: bool,
    ch_replace_table: bool,
) -> None:
    if not isinstance(if_not_exists, bool):
        raise TypeError("if_not_exists must be a boolean.")
    if drop_if_exists and if_not_exists:
        raise InvalidSqlInputError("drop_if_exists and if_not_exists cannot both be True.")
    if ch_replace_table and if_not_exists:
        raise InvalidSqlInputError("ch_replace_table and if_not_exists cannot both be True.")


def _handle_existing_create_target(
    *,
    config: Any,
    table_name: str,
    drop_if_exists: bool,
    if_not_exists: bool,
    ch_replace_table: bool,
    query_label: str | None,
    return_metadata: bool,
) -> object:
    if drop_if_exists or ch_replace_table or not if_not_exists:
        return _CREATE_CONTINUE
    from ..dml.table._basic_ops import table_exists

    connection = get_sql_connection(config.connection_key)
    try:
        exists = table_exists(
            config.backend,
            connection,
            table_name,
            config.connection_key,
        )
    finally:
        connection.close()
    if not exists:
        return _CREATE_CONTINUE
    if return_metadata:
        return SqlOperationResult(
            rows=None,
            metadata=SqlOperationMetadata(statement_count=0, query_label=query_label),
        )
    return None


def _create_sql_table_from_sql_source(
    *,
    db_key: str,
    table_name: str,
    sql: str,
    source_db: str | None,
    insert_data: bool,
    drop_target_if_exists: bool,
    gp_distributed_by_key: str | Sequence[str] | None,
    gp_partitions: Mapping[str, Any] | None,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_policy_overrides: Mapping[str, Any],
    ch_ddl_ready_timeout_seconds: float | None,
    ch_ddl_wait_policy: str | None,
    ch_only_shard: bool,
    retry_cnt: int,
    timeout_increment: float,
    dry_run: bool,
    return_sql: bool,
    only_generate_sql: bool,
    query_label: str | None,
    return_metadata: bool,
) -> str | int | SqlPlan | SqlOperationResult | None:
    target_config = get_connection_config(db_key)
    ch_engine = ch_policy_overrides.get("ch_engine")
    ch_cluster = ch_policy_overrides.get("ch_cluster")
    ch_sharding_key = ch_policy_overrides.get("ch_sharding_key")
    policy = None
    if not get_backend(target_config.backend).supports_distributed_table_targets():
        if ch_ddl_wait_policy is not None:
            message = "ch_ddl_wait_policy requires a ClickHouse target."
            raise ValueError(message)
        ch_engine = ch_engine or "ReplicatedMergeTree"
        ch_cluster = ch_cluster or "{cluster}"
        ch_sharding_key = ch_sharding_key or "rand()"
    else:
        policy = resolve_create_ch_policy(
            target_config,
            **ch_policy_overrides,
            ch_only_shard=ch_only_shard,
            ch_ddl_ready_timeout_seconds=ch_ddl_ready_timeout_seconds,
            ch_ddl_wait_policy=ch_ddl_wait_policy,
        )
        ch_engine = policy.shard_engine
        ch_cluster = policy.distributed_cluster or policy.shard_on_cluster or "{cluster}"
        ch_sharding_key = policy.sharding_key or "rand()"
    if only_generate_sql:
        from .query_source import generate_create_table_from_query_sql

        return generate_create_table_from_query_sql(
            source_db=source_db or db_key,
            table_db=db_key,
            table_name=table_name,
            sql=sql,
            gp_distributed_by_key=gp_distributed_by_key,
            gp_partitions=gp_partitions,
            partition_by=partition_by,
            order_by=order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_only_shard=ch_only_shard,
            drop_target_if_exists=drop_target_if_exists,
            query_label=query_label,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            get_connection_config_fn=get_connection_config,
            get_backend_adapter_fn=get_backend_adapter,
            get_sql_connection_fn=get_sql_connection,
        )

    from ..dml.table.create_table_from_sql import create_table_from_sql

    return create_table_from_sql(
        source_db or db_key,
        table_name,
        sql,
        table_db=db_key,
        insert_data=insert_data,
        drop_target_if_exists=drop_target_if_exists,
        gp_distributed_by_key=gp_distributed_by_key,
        gp_partitions=gp_partitions,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=None,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=getattr(policy, "create_distributed_pair", None),
        ch_distributed_engine_template=getattr(policy, "distributed_engine_template", None),
        ch_distributed_cluster=getattr(policy, "distributed_cluster", None),
        ch_shard_on_cluster=getattr(policy, "shard_on_cluster", None),
        ch_distributed_on_cluster=getattr(policy, "distributed_on_cluster", None),
        ch_ddl_ready_timeout_seconds=ch_ddl_ready_timeout_seconds,
        ch_ddl_wait_policy=ch_ddl_wait_policy,
        ch_only_shard=ch_only_shard,
        dry_run=dry_run,
        return_sql=return_sql,
        return_metadata=return_metadata,
        query_label=query_label,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
    )


def _format_sql_statements(sqls: Sequence[str]) -> str:
    return ";\n".join(statement.rstrip(";") for statement in sqls)


def _create_sql_table_with_connection(
    backend: str,
    connection: Any,
    table_name: str,
    df: pd.DataFrame | None = None,
    *,
    connection_key: str | None = None,
    gp_distributed_by_key: str | Sequence[str] | None = None,
    gp_partitions: Mapping[str, Any] | None = None,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str | None = None,
    ch_cluster: str | None = None,
    ch_sharding_key: str | None = None,
    ch_distributed_table: bool | None = None,
    ch_only_shard: bool = False,
    ch_replace_table: bool = False,
    dry_run: bool = False,
    return_sql: bool = False,
    query_label: str | None = None,
    return_metadata: bool = False,
    table_schema: Mapping[str, str] | None = None,
    ddl_scope: str = "regular",
    ddl_properties: Mapping[str, Any] | None = None,
    ch_creation_policy: Any = None,
) -> SqlPlan | SqlOperationResult | None:
    resolved_backend = resolve_connection_backend(backend)
    resolved_key = connection_key or resolved_backend
    is_clickhouse = get_backend_adapter(resolved_backend).supports_distributed_table_targets()
    if connection_key is not None and (
        ddl_properties is None or (is_clickhouse and ch_creation_policy is None)
    ):
        config = get_connection_config(connection_key)
        defaults = getattr(config, "ddl_defaults", None)
        if ddl_properties is None and defaults is not None and not is_clickhouse:
            ddl_properties = getattr(defaults, ddl_scope)
        if is_clickhouse and ch_creation_policy is None:
            ch_creation_policy = resolve_create_ch_policy(
                config,
                ddl_scope=ddl_scope,
                ch_engine=ch_engine,
                ch_cluster=ch_cluster,
                ch_sharding_key=ch_sharding_key,
                ch_distributed_table=ch_distributed_table,
                ch_only_shard=ch_only_shard,
                ch_distributed_engine_template=None,
                ch_distributed_cluster=None,
                ch_shard_on_cluster=None,
                ch_distributed_on_cluster=None,
                warn_ch_cluster=False,
            )
    options = _build_create_table_options(
        connection_key=resolved_key,
        backend=resolved_backend,
        table_name=table_name,
        df=df,
        table_schema=table_schema,
        gp_distributed_by_key=gp_distributed_by_key,
        gp_partitions=gp_partitions,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=ch_distributed_table,
        ch_only_shard=ch_only_shard,
        ch_replace_table=ch_replace_table,
        drop_target_if_exists=False,
        if_not_exists=getattr(
            get_backend_adapter(resolved_backend),
            "default_create_if_not_exists",
            False,
        ),
        dry_run=dry_run,
        return_sql=return_sql,
        query_label=query_label,
        return_metadata=return_metadata,
        ddl_properties=ddl_properties,
        ch_creation_policy=ch_creation_policy,
        option_owner="connection",
    )
    create_sqls = _build_create_sql_table_sqls(options, option_owner="connection")
    metadata, plan = _build_create_table_plan(options, [], create_sqls)

    if options.dry_run or options.return_sql:
        return plan

    _execute_create_sql_table(
        options=options,
        connection=connection,
        create_sqls=create_sqls,
        metadata=metadata,
        retry_attempt=None,
    )
    if options.return_metadata:
        return SqlOperationResult(rows=None, metadata=metadata, plan=plan)
    return None


def _build_create_table_options(
    *,
    connection_key: str,
    backend: str,
    table_name: str,
    df: pd.DataFrame | None,
    table_schema: Mapping[str, str] | None,
    gp_distributed_by_key: str | Sequence[str] | None,
    gp_partitions: Mapping[str, Any] | None,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str | None,
    ch_cluster: str | None,
    ch_sharding_key: str | None,
    ch_distributed_table: bool | None,
    ch_only_shard: bool,
    ch_replace_table: bool,
    drop_target_if_exists: bool,
    if_not_exists: bool,
    dry_run: bool,
    return_sql: bool,
    query_label: str | None,
    return_metadata: bool,
    ddl_properties: Mapping[str, Any] | None,
    ch_creation_policy: Any,
    option_owner: str,
) -> CreateSqlTableOptions:
    from ..dml.table.table_validation import normalize_key_columns

    if ch_creation_policy is not None:
        ch_engine = ch_creation_policy.shard_engine
        ch_cluster = (
            ch_creation_policy.distributed_cluster
            or ch_creation_policy.shard_on_cluster
            or "{cluster}"
        )
        ch_sharding_key = ch_creation_policy.sharding_key or "rand()"
        ch_distributed_table = ch_creation_policy.create_distributed_pair
    else:
        ch_engine = ch_engine or "ReplicatedMergeTree"
        ch_cluster = ch_cluster or "{cluster}"
        ch_sharding_key = ch_sharding_key or "rand()"
        ch_distributed_table = bool(ch_distributed_table)
    _validate_only_shard(backend, ch_only_shard, option_owner)
    create_df, normalized_schema = _resolve_create_dataframe_and_schema(
        df=df,
        table_schema=table_schema,
    )
    if table_name.strip() == "":
        raise ValueError("table_name must not be empty.")
    adapter = get_backend_adapter(backend)
    normalized_gp_partitions = adapter.normalize_gp_partitions_option(
        gp_partitions,
        partition_by=partition_by,
        option_owner=option_owner,
    )
    return CreateSqlTableOptions(
        connection_key=connection_key,
        backend=backend,
        table_name=table_name,
        df=create_df,
        table_schema=normalized_schema,
        gp_distributed_by_key=normalize_key_columns(
            gp_distributed_by_key,
            "gp_distributed_by_key",
        ),
        gp_partitions=normalized_gp_partitions,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=ch_distributed_table,
        ch_only_shard=ch_only_shard,
        ch_replace_table=ch_replace_table,
        drop_target_if_exists=drop_target_if_exists,
        if_not_exists=if_not_exists,
        dry_run=dry_run,
        return_sql=return_sql,
        query_label=query_label,
        return_metadata=return_metadata,
        ddl_properties=ddl_properties,
        ch_creation_policy=ch_creation_policy,
    )


def _resolve_create_dataframe_and_schema(
    *,
    df: pd.DataFrame | None,
    table_schema: Mapping[str, str] | None,
) -> tuple[pd.DataFrame, dict[str, str] | None]:
    if df is None:
        normalized_schema = normalize_table_schema(table_schema)
        if normalized_schema is None:
            raise InvalidSqlInputError(
                "Exactly one schema source must be provided: df, sql, or table_schema."
            )
        return pd.DataFrame(columns=list(normalized_schema)), normalized_schema

    if not isinstance(df, pd.DataFrame):
        raise TypeError("df must be a pandas DataFrame.")
    if table_schema is not None:
        raise InvalidSqlInputError(
            "Exactly one schema source must be provided: df, sql, or table_schema."
        )
    return df, None


def _build_create_sql_table_sqls(
    options: CreateSqlTableOptions,
    *,
    option_owner: str,
) -> list[str]:
    return _build_create_table_sqls(
        options.backend,
        options.table_name,
        options.df,
        table_schema=options.table_schema,
        gp_distributed_by_key=options.gp_distributed_by_key,
        gp_partitions=options.gp_partitions,
        partition_by=options.partition_by,
        order_by=options.order_by,
        ch_engine=options.ch_engine,
        ch_cluster=options.ch_cluster,
        ch_sharding_key=options.ch_sharding_key,
        ch_distributed_table=options.ch_distributed_table,
        ch_only_shard=options.ch_only_shard,
        ch_replace_table=options.ch_replace_table,
        if_not_exists=options.if_not_exists,
        query_label=options.query_label,
        option_owner=option_owner,
        ddl_properties=options.ddl_properties,
        ch_creation_policy=options.ch_creation_policy,
    )


def _build_create_table_plan(
    options: CreateSqlTableOptions,
    drop_sqls: list[str],
    create_sqls: list[str],
) -> tuple[SqlOperationMetadata, SqlPlan]:
    metadata = SqlOperationMetadata(
        statement_count=len(drop_sqls) + len(create_sqls),
        query_label=options.query_label,
    )
    plan = SqlPlan(
        operation="create_table",
        target_alias=options.connection_key,
        target_backend=options.backend,
        target_table=options.table_name,
        options={
            "drop_if_exists": options.drop_target_if_exists,
            "drop_target_if_exists": options.drop_target_if_exists,
            "if_not_exists": options.if_not_exists,
            "gp_partitions": _gp_partition_plan_option(options.gp_partitions),
            "ch_ddl_wait_policy": (
                options.ch_creation_policy.ddl_wait_policy
                if options.ch_creation_policy is not None
                else None
            ),
        },
        metadata=metadata,
    )
    plan.extend(
        drop_sqls,
        alias=options.connection_key,
        backend=options.backend,
        phase="drop_target",
        target_table=options.table_name,
    )
    plan.extend(
        create_sqls,
        alias=options.connection_key,
        backend=options.backend,
        phase="create_table",
        target_table=options.table_name,
    )
    return metadata, plan


def _execute_create_sql_table(
    *,
    options: CreateSqlTableOptions,
    connection: Any,
    create_sqls: list[str],
    metadata: SqlOperationMetadata,
    retry_attempt: int | None,
) -> None:
    time_print(
        f"Creating target table {options.table_name}",
        connection=options.connection_key,
        backend=options.backend,
    )
    with tracked_sql_operation(
        metadata=metadata,
        operation_name="create_table",
        alias=options.connection_key,
        backend=options.backend,
        phase="create_target",
        retry_attempt=retry_attempt,
        query_label=options.query_label,
        preview_sql=create_sqls[0] if create_sqls else None,
    ):
        adapter = get_backend_adapter(options.backend)
        expected_column_types = adapter.expected_create_table_column_types(
            options.df,
            options.table_schema,
            ch_distributed_table=options.ch_distributed_table,
            ch_only_shard=options.ch_only_shard,
        )
        adapter.execute_commands(connection, create_sqls)
        try:
            adapter.after_create_table(
                connection,
                options.table_name,
                ch_cluster=options.ch_cluster,
                ch_distributed_table=options.ch_distributed_table,
                ch_only_shard=options.ch_only_shard,
                expected_column_types=expected_column_types,
                ch_creation_policy=options.ch_creation_policy,
            )
        except TimeoutError as exc:
            raise SqlTableReadinessError(str(exc)) from exc


def _build_create_table_sqls(
    backend: str,
    table_name: str,
    df: pd.DataFrame,
    *,
    column_types: Mapping[str, str] | None = None,
    gp_distributed_by_key: str | Sequence[str] | None = None,
    gp_partitions: Mapping[str, Any] | Any | None = None,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_distributed_table: bool = False,
    ch_only_shard: bool = False,
    ch_replace_table: bool = False,
    if_not_exists: bool | None = None,
    query_label: str | None = None,
    table_schema: Mapping[str, str] | None = None,
    option_owner: str = "db_key",
    ddl_properties: Mapping[str, Any] | None = None,
    ch_creation_policy: Any = None,
) -> list[str]:
    from ..dml.table.table_validation import normalize_key_columns

    _validate_only_shard(backend, ch_only_shard, option_owner)
    resolved_column_types = (
        normalize_table_schema(table_schema, columns=df.columns)
        if table_schema is not None
        else column_types
    )
    joined_columns = _build_column_definitions(
        backend,
        df,
        resolved_column_types,
    )
    adapter = get_backend_adapter(backend)
    resolved_if_not_exists = (
        getattr(adapter, "default_create_if_not_exists", False)
        if if_not_exists is None
        else if_not_exists
    )
    normalized_gp_partitions = (
        gp_partitions
        if gp_partitions is not None and not isinstance(gp_partitions, Mapping)
        else adapter.normalize_gp_partitions_option(
            gp_partitions,
            partition_by=partition_by,
            option_owner=option_owner,
        )
    )
    if ch_creation_policy is not None:
        sqls = build_policy_create_sqls(
            table_name=table_name,
            joined_columns=joined_columns,
            partition_by=partition_by,
            order_by=order_by,
            policy=ch_creation_policy,
            ch_only_shard=ch_only_shard,
            ch_replace_table=ch_replace_table,
            if_not_exists=resolved_if_not_exists,
        )
    else:
        sqls = _build_backend_create_table_sqls(
            backend=backend,
            table_name=table_name,
            joined_columns=joined_columns,
            gp_distributed_by_key=normalize_key_columns(
                gp_distributed_by_key,
                "gp_distributed_by_key",
            ),
            gp_partitions=normalized_gp_partitions,
            partition_by=partition_by,
            order_by=order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_distributed_table=ch_distributed_table,
            ch_only_shard=ch_only_shard,
            ch_replace_table=ch_replace_table,
            if_not_exists=resolved_if_not_exists,
        )
    if ddl_properties:
        sqls = [overlay_with_properties(sql, ddl_properties) for sql in sqls]
    property_resolver = getattr(adapter, "explicit_create_property_overrides", None)
    explicit_properties = (
        property_resolver(partition_by, order_by) if property_resolver is not None else {}
    )
    if explicit_properties:
        sqls = [overlay_with_properties(sql, explicit_properties) for sql in sqls]
    return _apply_query_label_to_sqls(sqls, query_label)


def _gp_partition_plan_option(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if hasattr(value, "start"):
        return {
            "start": value.start,
            "end": value.end,
            "interval": value.interval,
        }
    return {"values": [partition.value for partition in value.partitions]}


def __getattr__(name: str) -> Any:
    if name == "create_sql_table":
        from .compat import create_sql_table

        return create_sql_table
    raise AttributeError(name)
