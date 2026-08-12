from __future__ import annotations

import warnings
from collections.abc import Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

import pandas as pd
import sqlparse

from analytics_toolkit.general import time_print

if TYPE_CHECKING:
    from collections.abc import Mapping

from analytics_toolkit.sql.ddl.api import _gp_partition_plan_option
from analytics_toolkit.sql.dml.ddl_options import resolve_operation_ddl

from ...backends import get_backend_adapter
from ...connection.config import get_connection_config
from ...connection.errors import (
    InvalidSqlInputError,
    SqlOperationContext,
    annotate_sql_exception,
    sql_preview,
)
from ...connection.get_sql_connection import get_sql_connection
from ...ddl.api import (
    _build_create_table_sqls,
    _create_sql_table_with_connection,
)
from ...ddl.schema import (
    normalize_table_schema,
    validate_table_schema_columns,
)
from ...execution.labels import apply_query_label
from ...execution.operation_runner import (
    run_retrying_operation,
    timed_public_sql_function,
    tracked_sql_operation,
    validate_retry_options,
)
from ...execution.plan_steps import (
    add_create_table_placeholder_step,
    add_create_table_steps,
    add_drop_target_steps,
    add_insert_query_step,
    add_inspect_schema_step,
)
from ...execution.plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
from ...execution.validation import validate_optional_positive_int
from ..transfer.schema import inspect_source_query_schema, map_source_schema_to_target
from ._basic_ops import (
    insert_from_query,
    table_exists,
)
from .models import CreateTableFromSqlOptions
from .table_validation import normalize_key_columns, validate_key_columns_in_columns


def transfer_table(**kwargs: Any) -> int | SqlPlan | SqlOperationResult:
    from ..transfer.flow.api import transfer_table as _transfer_table

    return _transfer_table(**kwargs)


@timed_public_sql_function
def create_table_from_sql(
    source_db: str,
    table_name: str,
    sql: str,
    *,
    table_db: str | None = None,
    insert_data: bool = True,
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
    ch_ddl_ready_timeout_seconds: float | None = None,
    ch_ddl_wait_policy: str | None = None,
    ch_only_shard: bool = False,
    ch_retry_per_host_drops: bool = True,
    trino_insert_chunk_size: int | None = None,
    retry_cnt: int = 5,
    timeout_increment: float = 5,
    dry_run: bool = False,
    return_sql: bool = False,
    return_metadata: bool = False,
    query_label: str | None = None,
    table_schema: dict[str, str] | None = None,
) -> int | None | SqlPlan | SqlOperationResult:
    validate_retry_options(retry_cnt, timeout_increment)
    validate_optional_positive_int(
        trino_insert_chunk_size,
        "trino_insert_chunk_size",
    )
    target_table = _normalize_table_name(table_name)
    source_sql = _normalize_single_query(sql)
    source_config = get_connection_config(source_db)
    target_config = source_config if table_db is None else get_connection_config(table_db)
    target_adapter = get_backend_adapter(target_config.backend)
    gp_distribution = normalize_key_columns(
        gp_distributed_by_key,
        "gp_distributed_by_key",
    )
    partition = target_adapter.normalize_ch_columns_or_expression(
        partition_by,
        "partition_by",
    )
    order = target_adapter.normalize_ch_columns_or_expression(order_by, "order_by")
    ch_only_shard = _normalize_only_shard(ch_only_shard)
    ddl = resolve_operation_ddl(
        target_config,
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
    ch_policy = ddl.regular_ch_policy
    ch_engine_name = ch_policy.shard_engine if ch_policy else ch_engine or "ReplicatedMergeTree"
    ch_cluster_name = (
        ch_policy.distributed_cluster or ch_policy.shard_on_cluster or "{cluster}"
        if ch_policy
        else ch_cluster or "{cluster}"
    )
    ch_sharding_key = (
        ch_policy.sharding_key or "rand()" if ch_policy else ch_sharding_key or "rand()"
    )
    normalized_gp_partitions = target_adapter.normalize_gp_partitions_option(
        gp_partitions,
        partition_by=partition,
        option_owner="table_db",
    )

    _validate_backend_options(
        target_backend=target_config.backend,
        gp_distributed_by_key=gp_distribution,
        partition_by=partition,
        order_by=order,
        ch_engine=ch_engine_name,
        ch_cluster=ch_cluster_name,
        ch_sharding_key=ch_sharding_key,
        ch_only_shard=ch_only_shard,
        trino_insert_chunk_size=trino_insert_chunk_size,
    )
    retry_per_host_drops = target_adapter.resolve_ch_retry_per_host_drops(
        bool(ch_retry_per_host_drops)
    )
    options = CreateTableFromSqlOptions(
        source_key=source_config.connection_key,
        source_backend=source_config.backend,
        target_key=target_config.connection_key,
        target_backend=target_config.backend,
        target_table=target_table,
        source_sql=source_sql,
        table_schema=normalize_table_schema(table_schema),
        insert_data=insert_data,
        drop_target_if_exists=drop_target_if_exists,
        gp_distributed_by_key=gp_distribution,
        gp_partitions=normalized_gp_partitions,
        partition_by=partition,
        order_by=order,
        ch_engine=ch_engine_name,
        ch_cluster=ch_cluster_name,
        ch_sharding_key=ch_sharding_key,
        ch_only_shard=ch_only_shard,
        ch_retry_per_host_drops=retry_per_host_drops,
        trino_insert_chunk_size=trino_insert_chunk_size,
        dry_run=dry_run,
        return_sql=return_sql,
        return_metadata=return_metadata,
        query_label=query_label,
        ddl_properties=ddl.regular_properties,
        ch_creation_policy=ch_policy,
    )

    if options.dry_run or options.return_sql:
        fast_path_applied, fast_path_result = _call_create_table_from_sql_fast_path(
            target_adapter=target_adapter,
            options=options,
        )
        if fast_path_applied:
            return cast("int | None | SqlPlan | SqlOperationResult", fast_path_result)
        return _build_create_table_from_sql_plan(
            source_key=options.source_key,
            source_backend=options.source_backend,
            target_key=options.target_key,
            target_backend=options.target_backend,
            target_table=options.target_table,
            source_sql=options.source_sql,
            table_schema=options.table_schema,
            insert_data=options.insert_data,
            drop_target_if_exists=options.drop_target_if_exists,
            gp_distributed_by_key=options.gp_distributed_by_key,
            gp_partitions=options.gp_partitions,
            partition_by=options.partition_by,
            order_by=options.order_by,
            ch_engine=options.ch_engine,
            ch_cluster=options.ch_cluster,
            ch_sharding_key=options.ch_sharding_key,
            ch_only_shard=options.ch_only_shard,
            query_label=options.query_label,
            ddl_properties=options.ddl_properties,
            ch_creation_policy=options.ch_creation_policy,
        )

    def execute_attempt(attempt: int) -> object:
        return _execute_create_table_from_sql_attempt(
            options=options,
            target_adapter=target_adapter,
            attempt=attempt,
        )

    result = run_retrying_operation(
        operation_name=(f"creating table {options.target_key}.{options.target_table} from query"),
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        operation=execute_attempt,
        context_factory=lambda attempt: SqlOperationContext(
            operation="create_table_from_sql",
            alias=options.target_key,
            backend=options.target_backend,
            phase="create_or_insert",
            target_table=options.target_table,
            retry_attempt=attempt,
            sql_preview=sql_preview(options.source_sql),
        ),
    )
    if isinstance(result, _UnsafeAttemptFailure):
        annotate_sql_exception(
            result.error,
            SqlOperationContext(
                operation="create_table_from_sql",
                alias=options.target_key,
                backend=options.target_backend,
                phase="create_or_insert",
                target_table=options.target_table,
                retry_attempt=result.attempt,
                sql_preview=sql_preview(options.source_sql),
            ),
        )
        raise result.error
    return cast("int | None | SqlPlan | SqlOperationResult", result)


class _UnsafeAttemptFailure:
    def __init__(self, error: Exception, attempt: int) -> None:
        self.error = error
        self.attempt = attempt


def _call_create_table_from_sql_fast_path(
    *,
    target_adapter: Any,
    options: CreateTableFromSqlOptions,
) -> tuple[bool, object]:
    return cast(
        "tuple[bool, object]",
        target_adapter.create_table_from_sql_fast_path(
            source_backend=options.source_backend,
            source_key=options.source_key,
            target_key=options.target_key,
            target_table=options.target_table,
            source_sql=options.source_sql,
            partition_by=options.partition_by,
            order_by=options.order_by,
            ch_engine=options.ch_engine,
            ch_cluster=options.ch_cluster,
            ch_sharding_key=options.ch_sharding_key,
            ch_only_shard=options.ch_only_shard,
            ch_retry_per_host_drops=options.ch_retry_per_host_drops,
            insert_data=options.insert_data,
            drop_target_if_exists=options.drop_target_if_exists,
            dry_run=options.dry_run,
            return_sql=options.return_sql,
            query_label=options.query_label,
            return_metadata=options.return_metadata,
            table_schema=options.table_schema,
        ),
    )


def _execute_create_table_from_sql_attempt(
    *,
    options: CreateTableFromSqlOptions,
    target_adapter: Any,
    attempt: int,
) -> object:
    if target_adapter.uses_create_table_from_sql_fast_path(
        source_backend=options.source_backend,
        source_key=options.source_key,
        target_key=options.target_key,
    ):
        return _execute_create_table_from_sql_fast_path_attempt(
            options=options,
            target_adapter=target_adapter,
            attempt=attempt,
        )
    return _execute_generic_create_table_from_sql_attempt(
        options=options,
        target_adapter=target_adapter,
        attempt=attempt,
    )


def _execute_create_table_from_sql_fast_path_attempt(
    *,
    options: CreateTableFromSqlOptions,
    target_adapter: Any,
    attempt: int,
) -> object:
    target_existed = False
    if not options.drop_target_if_exists:
        probe_connection = get_sql_connection(options.target_key)
        try:
            target_existed = table_exists(
                options.target_backend,
                probe_connection,
                options.target_table,
                options.target_key,
            )
        finally:
            _close_connection_quietly(
                probe_connection,
                connection_key=options.target_key,
                backend=options.target_backend,
            )
    owns_target = options.drop_target_if_exists or not target_existed
    try:
        fast_path_applied, fast_path_result = _call_create_table_from_sql_fast_path(
            target_adapter=target_adapter,
            options=options,
        )
        if not fast_path_applied:
            raise RuntimeError("Expected the ClickHouse create-table fast path to apply.")
        return fast_path_result
    except Exception as exc:
        if owns_target and _cleanup_attempt_target(
            options=options,
            target_adapter=target_adapter,
            target_connection=None,
        ):
            raise
        return _UnsafeAttemptFailure(exc, attempt)


def _execute_generic_create_table_from_sql_attempt(
    *,
    options: CreateTableFromSqlOptions,
    target_adapter: Any,
    attempt: int,
) -> object:
    source_connection: Any | None = None
    target_connection: Any | None = None
    inserted_rows: int | None = None
    delegate_transfer = False
    target_column_types: dict[str, str] = {}
    target_owned_by_attempt: bool | None = None
    mutation_started = False
    operation_metadata = SqlOperationMetadata(query_label=options.query_label)

    try:
        with tracked_sql_operation(
            metadata=operation_metadata,
            operation_name="create_table_from_sql",
            alias=options.target_key,
            backend=options.target_backend,
            phase="create_or_insert",
            retry_attempt=attempt,
            query_label=options.query_label,
            preview_sql=options.source_sql,
        ):
            source_connection = get_sql_connection(options.source_key)
            target_connection = (
                source_connection
                if options.source_key == options.target_key
                else get_sql_connection(options.target_key)
            )

            time_print(
                "Inspecting source query schema",
                connection=options.source_key,
                backend=options.source_backend,
            )
            source_schema = inspect_source_query_schema(
                options.source_backend,
                source_connection,
                apply_query_label(options.source_sql, options.query_label),
            )
            source_columns = [column.name for column in source_schema]
            _validate_source_columns(source_columns)
            validate_key_columns_in_columns(options.gp_distributed_by_key, source_columns)
            target_adapter.validate_ch_columns_in_columns(
                options.partition_by,
                source_columns,
                "partition_by",
                data_name="source query",
            )
            target_adapter.validate_ch_columns_in_columns(
                options.order_by,
                source_columns,
                "order_by",
                data_name="source query",
            )

            if options.table_schema is None:
                target_column_types = map_source_schema_to_target(
                    source_schema,
                    options.target_backend,
                    source_backend=options.source_backend,
                )
            else:
                target_column_types = validate_table_schema_columns(
                    options.table_schema,
                    source_columns,
                )
            target_existed_before_attempt = (
                False
                if options.drop_target_if_exists
                else table_exists(
                    options.target_backend,
                    target_connection,
                    options.target_table,
                    options.target_key,
                )
            )
            target_owned_by_attempt = not target_existed_before_attempt
            mutation_started = options.drop_target_if_exists
            target_exists_before_drop = target_adapter.prepare_existing_target_for_create_from_sql(
                target_connection,
                options.target_table,
                drop_target_if_exists=options.drop_target_if_exists,
                ch_cluster=options.ch_cluster,
                ch_only_shard=options.ch_only_shard,
                query_label=options.query_label,
                connection_key=options.target_key,
                ch_retry_per_host_drops=options.ch_retry_per_host_drops,
            )

            create_kwargs: dict[str, Any] = {
                "table_schema": target_column_types,
                "query_label": options.query_label,
                "ddl_properties": options.ddl_properties,
                "ch_creation_policy": options.ch_creation_policy,
            }
            create_kwargs.update(
                target_adapter.build_create_from_sql_target_create_kwargs(
                    gp_distributed_by_key=options.gp_distributed_by_key,
                    gp_partitions=options.gp_partitions,
                    partition_by=options.partition_by,
                    order_by=options.order_by,
                    ch_engine=options.ch_engine,
                    ch_cluster=options.ch_cluster,
                    ch_sharding_key=options.ch_sharding_key,
                    ch_only_shard=options.ch_only_shard,
                    drop_target_if_exists=options.drop_target_if_exists,
                    target_exists_before_drop=target_exists_before_drop,
                )
            )

            mutation_started = True
            _create_sql_table_with_connection(
                options.target_backend,
                target_connection,
                options.target_table,
                None,
                connection_key=options.target_key,
                **create_kwargs,
            )

            if not options.insert_data:
                if options.return_metadata:
                    return SqlOperationResult(
                        rows=None,
                        metadata=operation_metadata,
                    )
                return None

            if target_adapter.should_insert_create_table_from_sql_directly(
                source_backend=options.source_backend,
                source_key=options.source_key,
                target_key=options.target_key,
            ):
                inserted_rows = insert_from_query(
                    options.target_backend,
                    target_connection,
                    options.target_table,
                    options.source_sql,
                    target_column_types,
                    query_label=options.query_label,
                )
            else:
                delegate_transfer = True
    except Exception as exc:
        if not mutation_started:
            raise
        if target_owned_by_attempt and _cleanup_attempt_target(
            options=options,
            target_adapter=target_adapter,
            target_connection=target_connection,
        ):
            raise
        return _UnsafeAttemptFailure(exc, attempt)
    finally:
        _close_connections(
            source_connection=source_connection,
            source_key=options.source_key,
            source_backend=options.source_backend,
            target_connection=target_connection,
            target_key=options.target_key,
            target_backend=options.target_backend,
        )

    if delegate_transfer:
        transfer_kwargs: dict[str, object] = {
            "from_db": options.source_key,
            "to_db": options.target_key,
            "from_sql": options.source_sql,
            "to_table": options.target_table,
            "write_mode": "append",
            "gp_distributed_by_key": options.gp_distributed_by_key,
            "trino_insert_chunk_size": options.trino_insert_chunk_size,
            "partition_by": options.partition_by,
            "order_by": options.order_by,
            "ch_engine": options.ch_engine,
            "ch_sharding_key": options.ch_sharding_key,
            "retry_cnt": 1,
            "timeout_increment": 0,
            "full_retry_cnt": 1,
            "full_timeout_increment": 0,
        }
        if options.ch_creation_policy is not None:
            transfer_kwargs.update(
                {
                    "ch_shard_on_cluster": options.ch_creation_policy.shard_on_cluster,
                    "ch_distributed_on_cluster": (
                        options.ch_creation_policy.distributed_on_cluster
                    ),
                    "ch_distributed_cluster": (options.ch_creation_policy.distributed_cluster),
                }
            )
        if options.ch_only_shard:
            transfer_kwargs["ch_only_shard"] = True
        if options.query_label is not None:
            transfer_kwargs["query_label"] = options.query_label
        if options.table_schema is not None:
            transfer_kwargs["table_schema"] = target_column_types
        if options.return_metadata:
            transfer_kwargs["return_metadata"] = options.return_metadata
        try:
            return transfer_table(**transfer_kwargs)
        except Exception as exc:
            if target_owned_by_attempt and _cleanup_attempt_target(
                options=options,
                target_adapter=target_adapter,
                target_connection=None,
            ):
                raise
            return _UnsafeAttemptFailure(exc, attempt)
    if options.return_metadata:
        operation_metadata.source_rows = inserted_rows
        operation_metadata.inserted_rows = inserted_rows
        operation_metadata.affected_rows = inserted_rows
        return SqlOperationResult(
            rows=inserted_rows,
            metadata=operation_metadata,
        )
    return inserted_rows


def _cleanup_attempt_target(
    *,
    options: CreateTableFromSqlOptions,
    target_adapter: Any,
    target_connection: Any | None,
) -> bool:
    cleanup_error: Exception | None = None
    if target_connection is not None:
        try:
            target_adapter.rollback_quietly(target_connection)
            _drop_attempt_target(
                options=options,
                target_adapter=target_adapter,
                target_connection=target_connection,
            )
            return True
        except Exception as exc:
            cleanup_error = exc

    fresh_connection: Any | None = None
    try:
        fresh_connection = get_sql_connection(options.target_key)
        _drop_attempt_target(
            options=options,
            target_adapter=target_adapter,
            target_connection=fresh_connection,
        )
        cleanup_error = None
        return True
    except Exception as exc:
        cleanup_error = exc
        return False
    finally:
        if fresh_connection is not None:
            _close_connection_quietly(
                fresh_connection,
                connection_key=options.target_key,
                backend=options.target_backend,
            )
        if cleanup_error is not None:
            _warn_runtime_quietly(
                f"Could not remove partial target {options.target_table!r} after a "
                f"failed create-table attempt: {cleanup_error}",
                stacklevel=3,
            )


def _drop_attempt_target(
    *,
    options: CreateTableFromSqlOptions,
    target_adapter: Any,
    target_connection: Any,
) -> None:
    time_print(
        f"Removing partial target table {options.target_table}",
        connection=options.target_key,
        backend=options.target_backend,
        phase="cleanup",
    )
    target_adapter.prepare_existing_target_for_create_from_sql(
        target_connection,
        options.target_table,
        drop_target_if_exists=True,
        ch_cluster=options.ch_cluster,
        ch_only_shard=options.ch_only_shard,
        query_label=options.query_label,
        connection_key=options.target_key,
        ch_retry_per_host_drops=options.ch_retry_per_host_drops,
    )


def _normalize_table_name(table_name: str) -> str:
    normalized = table_name.strip()
    if not normalized:
        raise InvalidSqlInputError("table_name must not be empty.")
    return normalized


def _build_create_table_from_sql_plan(
    *,
    source_key: str,
    source_backend: str,
    target_key: str,
    target_backend: str,
    target_table: str,
    source_sql: str,
    table_schema: dict[str, str] | None,
    insert_data: bool,
    drop_target_if_exists: bool,
    gp_distributed_by_key: list[str] | None,
    gp_partitions: Any,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_only_shard: bool,
    query_label: str | None,
    ddl_properties: dict[str, Any] | None,
    ch_creation_policy: Any,
) -> SqlPlan:
    plan = SqlPlan(
        operation="create_table_from_sql",
        source_alias=source_key,
        target_alias=target_key,
        source_backend=source_backend,
        target_backend=target_backend,
        target_table=target_table,
        options={
            "insert_data": insert_data,
            "drop_target_if_exists": drop_target_if_exists,
            "table_schema": table_schema,
            "gp_distributed_by_key": gp_distributed_by_key,
            "gp_partitions": _gp_partition_plan_option(gp_partitions),
            "partition_by": partition_by,
            "order_by": order_by,
            "ch_only_shard": ch_only_shard,
            "ch_ddl_wait_policy": (
                ch_creation_policy.ddl_wait_policy if ch_creation_policy is not None else None
            ),
        },
    )
    add_inspect_schema_step(
        plan,
        alias=source_key,
        backend=source_backend,
        source_sql=source_sql,
        query_label=query_label,
    )
    if drop_target_if_exists:
        add_drop_target_steps(
            plan,
            alias=target_key,
            backend=target_backend,
            table_name=target_table,
            ch_cluster=ch_cluster,
            query_label=query_label,
            ch_only_shard=ch_only_shard,
        )
    if table_schema is None:
        add_create_table_placeholder_step(
            plan,
            alias=target_key,
            backend=target_backend,
            table_name=target_table,
            query_label=query_label,
        )
    else:
        create_kwargs = get_backend_adapter(
            target_backend
        ).build_create_from_sql_target_create_kwargs(
            gp_distributed_by_key=gp_distributed_by_key,
            gp_partitions=gp_partitions,
            partition_by=partition_by,
            order_by=order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_only_shard=ch_only_shard,
            drop_target_if_exists=drop_target_if_exists,
            target_exists_before_drop=False,
        )
        add_create_table_steps(
            plan,
            _build_create_table_sqls(
                target_backend,
                target_table,
                pd.DataFrame(columns=list(table_schema)),
                table_schema=table_schema,
                query_label=query_label,
                ddl_properties=ddl_properties,
                ch_creation_policy=ch_creation_policy,
                **create_kwargs,
            ),
            alias=target_key,
            backend=target_backend,
            table_name=target_table,
        )
    if insert_data:
        add_insert_query_step(
            plan,
            alias=target_key,
            backend=target_backend,
            target_table=target_table,
            source_sql=source_sql,
            phase="insert_data",
            query_label=query_label,
        )
    return plan


def _normalize_single_query(query: str) -> str:
    normalized = query.strip()
    if not normalized:
        raise InvalidSqlInputError("sql must not be empty.")

    statements = [
        statement.strip().rstrip(";").rstrip()
        for statement in sqlparse.split(normalized)
        if statement.strip()
    ]
    if len(statements) != 1:
        raise InvalidSqlInputError("create_table_from_sql expects exactly one SQL statement.")
    return statements[0]


def _validate_source_columns(columns: Sequence[str]) -> None:
    if not columns:
        raise ValueError("sql must return at least one column.")
    duplicates = [column for column in columns if columns.count(column) > 1]
    if duplicates:
        duplicated_columns = ", ".join(dict.fromkeys(duplicates))
        raise ValueError(f"sql must not return duplicate columns: {duplicated_columns}")


def _validate_backend_options(
    *,
    target_backend: str,
    gp_distributed_by_key: list[str] | None,
    partition_by: list[str] | str | None,
    order_by: list[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_only_shard: bool,
    trino_insert_chunk_size: int | None,
) -> None:
    target_adapter = get_backend_adapter(target_backend)
    target_adapter.validate_gp_distributed_by_key_option(
        gp_distributed_by_key,
        option_owner="table_db",
    )
    target_adapter.validate_trino_insert_chunk_size_option(
        trino_insert_chunk_size,
        option_owner="table_db",
    )
    target_adapter.validate_ch_create_table_options(
        option_owner="table_db",
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_only_shard=ch_only_shard,
    )


def _normalize_only_shard(ch_only_shard: bool) -> bool:
    if not isinstance(ch_only_shard, bool):
        raise ValueError("ch_only_shard must be a boolean.")
    return ch_only_shard


def _close_connections(
    *,
    source_connection: Any | None,
    source_key: str,
    source_backend: str,
    target_connection: Any | None,
    target_key: str,
    target_backend: str,
) -> None:
    if target_connection is not None and target_connection is not source_connection:
        _close_connection_quietly(
            target_connection,
            connection_key=target_key,
            backend=target_backend,
        )
    if source_connection is not None:
        _close_connection_quietly(
            source_connection,
            connection_key=source_key,
            backend=source_backend,
        )


def _close_connection_quietly(
    connection: Any,
    *,
    connection_key: str,
    backend: str,
) -> None:
    try:
        time_print(
            "Closing connection",
            connection=connection_key,
            backend=backend,
            phase="close",
        )
        connection.close()
    except Exception as exc:
        _warn_runtime_quietly(
            f"Could not close SQL connection {connection_key!r}: {exc}",
            stacklevel=3,
        )


def _warn_runtime_quietly(message: str, *, stacklevel: int) -> None:
    with suppress(RuntimeWarning):
        warnings.warn(message, RuntimeWarning, stacklevel=stacklevel)
