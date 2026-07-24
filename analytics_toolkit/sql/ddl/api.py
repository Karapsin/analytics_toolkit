from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from analytics_toolkit.general import time_print

from ..backend_adapters import get_backend_adapter
from analytics_toolkit.sql.backends import get_backend
from analytics_toolkit.sql.backends.ch.creation_policy import (
    build_policy_create_sqls,
    resolve_clickhouse_creation_policy,
)
from ..connection.config import get_connection_config, resolve_connection_backend
from analytics_toolkit.sql.connection.ddl_defaults import legacy_clickhouse_scope
from ..connection.errors import InvalidSqlInputError, SqlOperationContext, sql_preview
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
from .models import CreateSqlTableOptions
from .properties import overlay_with_properties
from .schema import (
    _build_column_definitions,
    normalize_table_schema,
)


@timed_public_sql_function
def create_sql_table(
    db_key: str,
    table_name: str,
    df: pd.DataFrame | None = None,
    *,
    sql: str | None = None,
    source_db: str | None = None,
    insert_data: bool = False,
    drop_target_if_exists: bool = False,
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
) -> str | SqlPlan | SqlOperationResult | int | None:
    validate_retry_options(retry_cnt, timeout_increment)
    _validate_create_schema_sources(
        df=df,
        sql=sql,
        table_schema=table_schema,
    )
    if sql is not None:
        return _create_sql_table_from_sql_source(
            db_key=db_key,
            table_name=table_name,
            sql=sql,
            source_db=source_db,
            insert_data=insert_data,
            drop_target_if_exists=drop_target_if_exists,
            gp_distributed_by_key=gp_distributed_by_key,
            gp_partitions=gp_partitions,
            partition_by=partition_by,
            order_by=order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_only_shard=ch_only_shard,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            dry_run=dry_run,
            return_sql=return_sql,
            only_generate_sql=only_generate_sql,
            query_label=query_label,
            return_metadata=return_metadata,
        )

    config = get_connection_config(db_key)
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
        dry_run=dry_run,
        return_sql=return_sql,
        query_label=query_label,
        return_metadata=return_metadata,
        ddl_properties=_regular_ddl_properties(config),
        ch_creation_policy=_resolve_create_ch_policy(
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
        ),
        option_owner="db_key",
    )
    create_sqls = _build_create_sql_table_sqls(options, option_owner="db_key")
    metadata, plan = _build_create_table_plan(options, create_sqls)

    if only_generate_sql:
        return _format_sql_statements(create_sqls)
    if options.dry_run or options.return_sql:
        return plan

    def operation(connection_ref: dict[str, Any], attempt: int) -> None:
        _execute_create_sql_table(
            options=options,
            connection=connection_ref["connection"],
            create_sqls=create_sqls,
            metadata=metadata,
            retry_attempt=attempt,
        )

    def context(attempt: int) -> SqlOperationContext:
        return SqlOperationContext(
            operation="create_sql_table",
            alias=options.connection_key,
            backend=options.backend,
            phase="create_target",
            target_table=options.table_name,
            retry_attempt=attempt,
            sql_preview=sql_preview(create_sqls[0] if create_sqls else options.table_name),
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
    ch_engine: str | None,
    ch_cluster: str | None,
    ch_sharding_key: str | None,
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
    if not get_backend(target_config.backend).supports_distributed_table_targets():
        ch_engine = ch_engine or "ReplicatedMergeTree"
        ch_cluster = ch_cluster or "{cluster}"
        ch_sharding_key = ch_sharding_key or "rand()"
    else:
        policy = _resolve_create_ch_policy(
            target_config,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_distributed_table=None,
            ch_only_shard=ch_only_shard,
            ch_distributed_engine_template=None,
            ch_distributed_cluster=None,
            ch_shard_on_cluster=None,
            ch_distributed_on_cluster=None,
        )
        ch_engine = policy.shard_engine
        ch_cluster = policy.distributed_cluster or policy.shard_on_cluster or "{cluster}"
        ch_sharding_key = policy.sharding_key or "rand()"
    if only_generate_sql:
        return _generate_create_sql_table_from_query_sql(
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
            query_label=query_label,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
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
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_only_shard=ch_only_shard,
        dry_run=dry_run,
        return_sql=return_sql,
        return_metadata=return_metadata,
        query_label=query_label,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
    )


def _generate_create_sql_table_from_query_sql(
    *,
    source_db: str,
    table_db: str,
    table_name: str,
    sql: str,
    gp_distributed_by_key: str | Sequence[str] | None,
    gp_partitions: Mapping[str, Any] | None,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_only_shard: bool,
    query_label: str | None,
    retry_cnt: int,
    timeout_increment: float,
) -> str:
    from ..dml.table.create_table_from_sql import (
        _normalize_only_shard,
        _normalize_single_query,
        _validate_source_columns,
    )
    from ..dml.table.table_validation import (
        normalize_key_columns,
        validate_key_columns_in_columns,
    )
    from ..dml.transfer.schema import (
        inspect_source_query_schema,
        map_source_schema_to_target,
    )
    from ..execution.labels import apply_query_label

    source_config = get_connection_config(source_db)
    target_config = get_connection_config(table_db)
    target_adapter = get_backend_adapter(target_config.backend)
    source_sql = _normalize_single_query(sql)
    gp_distribution = normalize_key_columns(
        gp_distributed_by_key,
        "gp_distributed_by_key",
    )
    partition = target_adapter.normalize_ch_columns_or_expression(
        partition_by,
        "partition_by",
    )
    order = target_adapter.normalize_ch_columns_or_expression(order_by, "order_by")
    ch_engine_name = target_adapter.normalize_ch_string(ch_engine, "ch_engine")
    ch_cluster_name = target_adapter.normalize_ch_string(ch_cluster, "ch_cluster")
    ch_sharding_key_name = target_adapter.normalize_ch_string(
        ch_sharding_key,
        "ch_sharding_key",
    )
    only_shard = _normalize_only_shard(ch_only_shard)
    normalized_gp_partitions = target_adapter.normalize_gp_partitions_option(
        gp_partitions,
        partition_by=partition,
        option_owner="db_key",
    )

    target_adapter.validate_gp_distributed_by_key_option(
        gp_distribution,
        option_owner="db_key",
    )
    target_adapter.validate_ch_create_table_options(
        option_owner="db_key",
        partition_by=partition,
        order_by=order,
        ch_engine=ch_engine_name,
        ch_cluster=ch_cluster_name,
        ch_sharding_key=ch_sharding_key_name,
        ch_only_shard=only_shard,
    )

    def inspect_schema(attempt: int) -> list[Any]:
        del attempt
        source_connection = get_sql_connection(source_config.connection_key)
        try:
            return inspect_source_query_schema(
                source_config.backend,
                source_connection,
                apply_query_label(source_sql, query_label),
            )
        finally:
            source_connection.close()

    from ..execution.operation_runner import run_retrying_operation

    source_schema = run_retrying_operation(
        operation_name=(
            f"inspecting query schema on {source_config.connection_key} ({source_config.backend})"
        ),
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        operation=inspect_schema,
        context_factory=lambda attempt: SqlOperationContext(
            operation="create_sql_table",
            alias=source_config.connection_key,
            backend=source_config.backend,
            phase="inspect_schema",
            target_table=table_name,
            retry_attempt=attempt,
            sql_preview=sql_preview(source_sql),
        ),
    )

    source_columns = [column.name for column in source_schema]
    _validate_source_columns(source_columns)
    validate_key_columns_in_columns(gp_distribution, source_columns)
    target_adapter.validate_ch_columns_in_columns(
        partition,
        source_columns,
        "partition_by",
        data_name="source query",
    )
    target_adapter.validate_ch_columns_in_columns(
        order,
        source_columns,
        "order_by",
        data_name="source query",
    )
    target_column_types = map_source_schema_to_target(
        source_schema,
        target_config.backend,
        source_backend=source_config.backend,
    )
    create_kwargs = target_adapter.build_create_from_sql_target_create_kwargs(
        gp_distributed_by_key=gp_distribution,
        gp_partitions=normalized_gp_partitions,
        partition_by=partition,
        order_by=order,
        ch_engine=ch_engine_name,
        ch_cluster=ch_cluster_name,
        ch_sharding_key=ch_sharding_key_name,
        ch_only_shard=only_shard,
        drop_target_if_exists=False,
        target_exists_before_drop=False,
    )
    create_sqls = _build_create_table_sqls(
        target_config.backend,
        table_name,
        pd.DataFrame(columns=source_columns),
        table_schema=target_column_types,
        query_label=query_label,
        option_owner="db_key",
        ddl_properties=_regular_ddl_properties(target_config),
        **create_kwargs,
    )
    return _format_sql_statements(create_sqls)


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
            scope = (
                getattr(defaults, ddl_scope)
                if defaults is not None
                else legacy_clickhouse_scope(staging=ddl_scope == "staging")
            )
            ch_creation_policy = resolve_clickhouse_creation_policy(
                scope,
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
        dry_run=dry_run,
        return_sql=return_sql,
        query_label=query_label,
        return_metadata=return_metadata,
        ddl_properties=ddl_properties,
        ch_creation_policy=ch_creation_policy,
        option_owner="connection",
    )
    create_sqls = _build_create_sql_table_sqls(options, option_owner="connection")
    metadata, plan = _build_create_table_plan(options, create_sqls)

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
        query_label=options.query_label,
        option_owner=option_owner,
        ddl_properties=options.ddl_properties,
        ch_creation_policy=options.ch_creation_policy,
    )


def _build_create_table_plan(
    options: CreateSqlTableOptions,
    create_sqls: list[str],
) -> tuple[SqlOperationMetadata, SqlPlan]:
    metadata = SqlOperationMetadata(
        statement_count=len(create_sqls),
        query_label=options.query_label,
    )
    plan = SqlPlan(
        operation="create_table",
        target_alias=options.connection_key,
        target_backend=options.backend,
        target_table=options.table_name,
        options={
            "gp_partitions": _gp_partition_plan_option(options.gp_partitions),
        },
        metadata=metadata,
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
        operation_name="create_sql_table",
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
        adapter.after_create_table(
            connection,
            options.table_name,
            ch_cluster=options.ch_cluster,
            ch_distributed_table=options.ch_distributed_table,
            ch_only_shard=options.ch_only_shard,
            expected_column_types=expected_column_types,
        )


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


def _resolve_create_ch_policy(config: Any, **overrides: Any) -> Any:
    if not get_backend_adapter(config.backend).supports_distributed_table_targets():
        return None
    defaults = getattr(config, "ddl_defaults", None)
    scope = defaults.regular if defaults is not None else legacy_clickhouse_scope()
    return resolve_clickhouse_creation_policy(scope, **overrides)


def _regular_ddl_properties(config: Any) -> Mapping[str, Any] | None:
    defaults = getattr(config, "ddl_defaults", None)
    return None if defaults is None else defaults.regular


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
