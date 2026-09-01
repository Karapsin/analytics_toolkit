from __future__ import annotations

# ruff: noqa: ARG001, BLE001, EM101, EM102, PLR0913, PYI041, TC003, TID252, TRY003
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import pandas as pd

from ...backends import get_backend_adapter
from ...backends.utils import extract_row_count
from ...connection.config import get_connection_config
from ...connection.errors import (
    InvalidSqlInputError,
    SqlOperationContext,
    annotate_sql_exception,
    sql_preview,
)
from ...connection.get_sql_connection import get_sql_connection
from ...ddl.api import _build_create_table_sqls, _gp_partition_plan_option
from ...execution.labels import apply_query_label
from ...execution.operation_runner import (
    timed_public_sql_function,
    tracked_sql_operation,
    validate_retry_options,
)
from ...execution.plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
from ..ddl_options import resolve_operation_ddl
from ..table.create_table_from_sql import _validate_backend_options, _validate_source_columns
from ..table.table_validation import normalize_key_columns, validate_key_columns_in_columns
from ..transfer.runtime.retry import is_non_retryable_sql_error, run_with_retry
from ..transfer.schema import inspect_source_query_schema, map_source_schema_to_target
from .execute_safety import (
    AmbiguousSqlMutationError,
    ExecuteAttemptState,
    ExecuteRetryPolicy,
    TrackingConnection,
    validate_execute_retry_policy,
)
from .execute_sql import (
    _iterate_statements_with_progress,
    _maybe_print_query,
    _rollback_confirmed,
    _validate_progress,
)
from .query_writes import (
    _join_statements,
    _normalize_result_statements,
    _normalize_target_table,
    _validate_result_query,
    _validate_target_table,
)


@dataclass(frozen=True)
class _ExecuteCreateOptions:
    connection_key: str
    backend: str
    table_name: str
    setup_sqls: list[str]
    source_sql: str
    create_sqls: list[str]
    drop_sqls: list[str]
    insert_after_create: bool
    gp_distributed_by_key: list[str] | None
    gp_partitions: Any
    partition_by: list[str] | str | None
    order_by: list[str] | str | None
    ddl_properties: Mapping[str, Any] | None
    ch_creation_policy: Any
    ch_only_shard: bool
    drop_if_exists: bool
    if_not_exists: bool
    print_queries: bool
    gp_break_query: bool
    gp_commit_each_statement: bool
    retry_cnt: int
    timeout_increment: int | float
    query_label: str | None
    return_metadata: bool
    progress: bool
    retry_policy: ExecuteRetryPolicy


@timed_public_sql_function
def execute_create(
    db_key: str,
    table_name: str,
    query: str,
    *,
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
    print_queries: bool = False,
    gp_break_query: bool = False,
    gp_commit_each_statement: bool = False,
    retry_cnt: int = 5,
    timeout_increment: int | float = 5,
    query_label: str | None = None,
    dry_run: bool = False,
    return_sql: bool = False,
    return_metadata: bool = False,
    progress: bool = False,
    retry_policy: ExecuteRetryPolicy = "safe",
) -> int | SqlPlan | SqlOperationResult:
    """Execute setup SQL and create a table from the final query on one connection."""
    options = _build_execute_create_options(
        db_key=db_key,
        table_name=table_name,
        query=query,
        drop_if_exists=drop_if_exists,
        if_not_exists=if_not_exists,
        gp_distributed_by_key=gp_distributed_by_key,
        gp_partitions=gp_partitions,
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
        print_queries=print_queries,
        gp_break_query=gp_break_query,
        gp_commit_each_statement=gp_commit_each_statement,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
        return_metadata=return_metadata,
        progress=progress,
        retry_policy=retry_policy,
    )
    plan = _build_execute_create_plan(options)
    if dry_run or return_sql:
        return plan
    return _execute_create_options(options, plan)


def _build_execute_create_options(**values: Any) -> _ExecuteCreateOptions:
    validate_retry_options(values["retry_cnt"], values["timeout_increment"])
    _validate_progress(values["progress"])
    retry_policy = validate_execute_retry_policy(values["retry_policy"])
    drop_if_exists = _validate_bool(values["drop_if_exists"], "drop_if_exists")
    if_not_exists = _validate_bool(values["if_not_exists"], "if_not_exists")
    if drop_if_exists and if_not_exists:
        raise InvalidSqlInputError("drop_if_exists and if_not_exists cannot both be True.")
    ch_only_shard = _validate_bool(values["ch_only_shard"], "ch_only_shard")

    statements = _normalize_result_statements(values["query"])
    source_sql = statements[-1]
    _validate_result_query(source_sql)
    config = get_connection_config(values["db_key"])
    adapter = get_backend_adapter(config.backend)
    table_name = _normalize_target_table(values["table_name"])
    _validate_target_table(table_name, config.backend)
    distribution = normalize_key_columns(
        values["gp_distributed_by_key"],
        "gp_distributed_by_key",
    )
    partition = adapter.normalize_ch_columns_or_expression(values["partition_by"], "partition_by")
    order = adapter.normalize_ch_columns_or_expression(values["order_by"], "order_by")
    ddl = resolve_operation_ddl(
        config,
        ch_engine=values["ch_engine"],
        ch_cluster=values["ch_cluster"],
        ch_sharding_key=values["ch_sharding_key"],
        ch_distributed_table=values["ch_distributed_table"],
        ch_only_shard=ch_only_shard,
        ch_distributed_engine_template=values["ch_distributed_engine_template"],
        ch_distributed_cluster=values["ch_distributed_cluster"],
        ch_shard_on_cluster=values["ch_shard_on_cluster"],
        ch_distributed_on_cluster=values["ch_distributed_on_cluster"],
        ch_ddl_ready_timeout_seconds=values["ch_ddl_ready_timeout_seconds"],
        ch_ddl_wait_policy=values["ch_ddl_wait_policy"],
    )
    policy = ddl.regular_ch_policy
    ch_engine_name = policy.shard_engine if policy else values["ch_engine"] or "ReplicatedMergeTree"
    ch_cluster_name = (
        policy.distributed_cluster or policy.shard_on_cluster or "{cluster}"
        if policy
        else values["ch_cluster"] or "{cluster}"
    )
    ch_sharding_key = (
        policy.sharding_key or "rand()" if policy else values["ch_sharding_key"] or "rand()"
    )
    _validate_backend_options(
        target_backend=config.backend,
        gp_distributed_by_key=distribution,
        partition_by=partition,
        order_by=order,
        ch_engine=ch_engine_name,
        ch_cluster=ch_cluster_name,
        ch_sharding_key=ch_sharding_key,
        ch_only_shard=ch_only_shard,
        trino_insert_chunk_size=None,
    )
    normalized_gp_partitions = adapter.normalize_gp_partitions_option(
        values["gp_partitions"],
        partition_by=partition,
        option_owner="db_key",
    )
    create_sqls, insert_after_create = _build_direct_create_sqls(
        backend=config.backend,
        table_name=table_name,
        source_sql=source_sql,
        gp_distributed_by_key=distribution,
        gp_partitions=normalized_gp_partitions,
        partition_by=partition,
        order_by=order,
        ddl_properties=ddl.regular_properties,
        ch_creation_policy=policy,
        ch_only_shard=ch_only_shard,
        if_not_exists=if_not_exists,
    )
    drop_sqls = (
        adapter.build_drop_target_sqls(
            table_name,
            ch_cluster=ch_cluster_name,
            ch_only_shard=ch_only_shard,
        )
        if drop_if_exists
        else []
    )

    def prepare(sql: str) -> str:
        return cast(
            "str",
            adapter.prepare_sql(config, apply_query_label(sql, values["query_label"])),
        )

    return _ExecuteCreateOptions(
        connection_key=config.connection_key,
        backend=config.backend,
        table_name=table_name,
        setup_sqls=[prepare(sql) for sql in statements[:-1]],
        source_sql=source_sql,
        create_sqls=[prepare(sql) for sql in create_sqls],
        drop_sqls=[prepare(sql) for sql in drop_sqls],
        insert_after_create=insert_after_create,
        gp_distributed_by_key=distribution,
        gp_partitions=normalized_gp_partitions,
        partition_by=partition,
        order_by=order,
        ddl_properties=ddl.regular_properties,
        ch_creation_policy=policy,
        ch_only_shard=ch_only_shard,
        drop_if_exists=drop_if_exists,
        if_not_exists=if_not_exists,
        print_queries=values["print_queries"],
        gp_break_query=values["gp_break_query"],
        gp_commit_each_statement=values["gp_commit_each_statement"],
        retry_cnt=values["retry_cnt"],
        timeout_increment=values["timeout_increment"],
        query_label=values["query_label"],
        return_metadata=values["return_metadata"],
        progress=values["progress"],
        retry_policy=retry_policy,
    )


def _build_direct_create_sqls(
    *,
    backend: str,
    table_name: str,
    source_sql: str,
    gp_distributed_by_key: list[str] | None,
    gp_partitions: Any,
    partition_by: list[str] | str | None,
    order_by: list[str] | str | None,
    ddl_properties: Mapping[str, Any] | None,
    ch_creation_policy: Any,
    ch_only_shard: bool,
    if_not_exists: bool,
) -> tuple[list[str], bool]:
    return cast(
        "tuple[list[str], bool]",
        get_backend_adapter(backend).build_execute_create_as_sqls(
            table_name=table_name,
            source_sql=source_sql,
            gp_distributed_by_key=gp_distributed_by_key,
            gp_partitions=gp_partitions,
            partition_by=partition_by,
            order_by=order_by,
            ddl_properties=ddl_properties,
            ch_creation_policy=ch_creation_policy,
            ch_only_shard=ch_only_shard,
            if_not_exists=if_not_exists,
        ),
    )


def _build_execute_create_plan(options: _ExecuteCreateOptions) -> SqlPlan:
    insert_sql = _prepared_insert_sql(options) if options.insert_after_create else None
    statement_count = (
        len(options.setup_sqls)
        + len(options.drop_sqls)
        + len(options.create_sqls)
        + int(insert_sql is not None)
    )
    metadata = SqlOperationMetadata(
        statement_count=statement_count,
        query_label=options.query_label,
    )
    plan = SqlPlan(
        operation="execute_create",
        target_alias=options.connection_key,
        target_backend=options.backend,
        target_table=options.table_name,
        options={
            "drop_if_exists": options.drop_if_exists,
            "if_not_exists": options.if_not_exists,
            "retry_policy": options.retry_policy,
            "gp_partitions": _gp_partition_plan_option(options.gp_partitions),
        },
        metadata=metadata,
    )
    plan.extend(options.setup_sqls, phase="setup")
    plan.extend(options.drop_sqls, phase="drop_target", target_table=options.table_name)
    plan.extend(options.create_sqls, phase="create_table", target_table=options.table_name)
    if insert_sql is not None:
        plan.add(insert_sql, phase="insert_target", target_table=options.table_name)
    for statement in plan.statements:
        object.__setattr__(statement, "alias", options.connection_key)
        object.__setattr__(statement, "backend", options.backend)
    return plan


def _execute_create_options(
    options: _ExecuteCreateOptions,
    plan: SqlPlan,
) -> int | SqlOperationResult:
    metadata = plan.metadata
    adapter = get_backend_adapter(options.backend)

    def operation(attempt: int) -> int:
        connection: Any | None = None
        state = ExecuteAttemptState()
        try:
            connection = get_sql_connection(options.connection_key)
            if options.if_not_exists and adapter.table_exists(
                connection,
                options.table_name,
                connection_key=options.connection_key,
            ):
                metadata.statement_count = 0
                metadata.affected_rows = 0
                metadata.inserted_rows = 0
                return 0
            execution_connection = connection
            if bool(getattr(adapter, "supports_transactions", False)):
                execution_connection = TrackingConnection(connection, state)
            with tracked_sql_operation(
                metadata=metadata,
                operation_name="execute_create",
                alias=options.connection_key,
                backend=options.backend,
                phase="create_table",
                retry_attempt=attempt,
                query_label=options.query_label,
                preview_sql=options.source_sql,
            ):
                result = _execute_create_attempt(options, adapter, execution_connection, state)
                metadata.affected_rows = result
                metadata.inserted_rows = result
                return result
        except Exception as exc:
            context = _execute_create_context(options, attempt)
            annotate_sql_exception(exc, context)
            if connection is None or options.retry_policy == "always":
                if connection is not None:
                    adapter.rollback_quietly(connection)
                raise
            if is_non_retryable_sql_error(exc):
                adapter.rollback_quietly(connection)
                raise
            if (
                bool(getattr(adapter, "supports_transactions", False))
                and not state.commit_started
                and not state.committed
                and _rollback_confirmed(connection)
            ):
                raise
            raise AmbiguousSqlMutationError(
                "Table creation may have completed before the connection failed; "
                "the toolkit will not replay it automatically.",
                context=context,
                original_error=exc,
            ) from exc
        finally:
            _close_connection_quietly(connection, options)

    result = cast(
        "int",
        run_with_retry(
            operation_name=(
                f"creating table {options.connection_key}.{options.table_name} "
                f"({options.backend}) from final query"
            ),
            retry_cnt=1 if options.retry_policy == "never" else options.retry_cnt,
            timeout_increment=options.timeout_increment,
            operation=operation,
        ),
    )
    if options.return_metadata:
        return SqlOperationResult(rows=result, metadata=metadata, plan=plan)
    return result


def _execute_create_attempt(
    options: _ExecuteCreateOptions,
    adapter: Any,
    connection: Any,
    state: ExecuteAttemptState,
) -> int:
    if not bool(getattr(adapter, "supports_transactions", False)):
        state.submitted = True
    if options.setup_sqls:
        adapter.execute_sql(
            connection,
            _join_statements(options.setup_sqls),
            print_queries=options.print_queries,
            gp_break_query=options.gp_break_query,
            gp_commit_each_statement=options.gp_commit_each_statement,
            progress=options.progress,
        )
    adapter.execute_commands(connection, options.drop_sqls)
    create_sqls = options.create_sqls
    if getattr(adapter, "requires_execute_create_schema_inference", False) and (
        options.gp_partitions is not None
    ):
        create_sqls = _build_gp_partition_create_sqls(options, connection)
    for sql in _iterate_statements_with_progress(
        create_sqls,
        options.backend,
        progress=options.progress,
    ):
        _maybe_print_query(sql, options.print_queries, split_preview=False)
    result: Any = None
    if len(create_sqls) != 1:
        adapter.execute_commands(connection, create_sqls)
    else:
        result = adapter.execute_command(connection, create_sqls[0])
    adapter.after_create_table(
        connection,
        options.table_name,
        ch_cluster=(
            options.ch_creation_policy.distributed_cluster
            or options.ch_creation_policy.shard_on_cluster
            or "{cluster}"
            if options.ch_creation_policy is not None
            else "{cluster}"
        ),
        ch_distributed_table=bool(
            options.ch_creation_policy is not None
            and options.ch_creation_policy.create_distributed_pair
        ),
        ch_only_shard=options.ch_only_shard,
        ch_creation_policy=options.ch_creation_policy,
    )
    if options.insert_after_create:
        result = adapter.execute_command(connection, _prepared_insert_sql(options))
    return extract_row_count(result)


def _build_gp_partition_create_sqls(
    options: _ExecuteCreateOptions,
    connection: Any,
) -> list[str]:
    source_schema = inspect_source_query_schema(
        options.backend,
        connection,
        apply_query_label(options.source_sql, options.query_label),
    )
    source_columns = [column.name for column in source_schema]
    _validate_source_columns(source_columns)
    validate_key_columns_in_columns(options.gp_distributed_by_key, source_columns)
    column_types = map_source_schema_to_target(
        source_schema,
        options.backend,
        source_backend=options.backend,
    )
    return _build_create_table_sqls(
        options.backend,
        options.table_name,
        pd.DataFrame(columns=source_columns),
        table_schema=column_types,
        gp_distributed_by_key=options.gp_distributed_by_key,
        gp_partitions=options.gp_partitions,
        partition_by=options.partition_by,
        order_by=options.order_by,
        if_not_exists=options.if_not_exists,
        query_label=options.query_label,
        ddl_properties=options.ddl_properties,
    )


def _prepared_insert_sql(options: _ExecuteCreateOptions) -> str:
    config = get_connection_config(options.connection_key)
    sql = apply_query_label(
        f"INSERT INTO {options.table_name}\n{options.source_sql}",
        options.query_label,
    )
    return cast("str", get_backend_adapter(options.backend).prepare_sql(config, sql))


def _execute_create_context(
    options: _ExecuteCreateOptions,
    attempt: int,
) -> SqlOperationContext:
    return SqlOperationContext(
        operation="execute_create",
        alias=options.connection_key,
        backend=options.backend,
        phase="create_table",
        target_table=options.table_name,
        retry_attempt=attempt,
        sql_preview=sql_preview(options.source_sql),
    )


def _close_connection_quietly(connection: Any | None, options: _ExecuteCreateOptions) -> None:
    if connection is None:
        return
    try:
        connection.close()
    except Exception:
        return


def _validate_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean.")
    return value


__all__ = ["execute_create"]
