from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd
import sqlparse

from ...backend_adapters import get_backend_adapter
from ...backends.ch.create_table_as import ch_create_table_as
from ...clickhouse.options import (
    normalize_ch_columns_or_expression,
    normalize_ch_string,
    validate_ch_columns_in_columns,
    validate_ch_options_not_used,
)
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
from ...execution.operation_runner import timed_public_sql_function, tracked_sql_operation
from ...execution.plan_steps import (
    add_create_table_steps,
    add_create_table_placeholder_step,
    add_drop_target_steps,
    add_inspect_schema_step,
    add_insert_query_step,
)
from ...execution.plans import SqlOperationMetadata, SqlOperationResult, SqlPlan
from ..transfer.schema import inspect_source_query_schema, map_source_schema_to_target
from ._basic_ops import (
    insert_from_query,
    table_exists,
)
from .models import CreateTableFromSqlOptions
from .table_validation import normalize_key_columns, validate_key_columns_in_columns
from analytics_toolkit.general import time_print


def transfer_table(**kwargs: Any) -> int:
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
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_only_shard: bool = False,
    ch_retry_per_host_drops: bool = True,
    trino_insert_chunk_size: int | None = None,
    dry_run: bool = False,
    return_sql: bool = False,
    return_metadata: bool = False,
    query_label: str | None = None,
    table_schema: dict[str, str] | None = None,
) -> int | None | SqlPlan | SqlOperationResult:
    target_table = _normalize_table_name(table_name)
    source_sql = _normalize_single_query(sql)
    source_config = get_connection_config(source_db)
    target_config = (
        source_config
        if table_db is None
        else get_connection_config(table_db)
    )
    gp_distribution = normalize_key_columns(
        gp_distributed_by_key,
        "gp_distributed_by_key",
    )
    partition = normalize_ch_columns_or_expression(
        partition_by,
        "partition_by",
    )
    order = normalize_ch_columns_or_expression(order_by, "order_by")
    ch_engine_name = normalize_ch_string(ch_engine, "ch_engine")
    ch_cluster_name = normalize_ch_string(ch_cluster, "ch_cluster")
    ch_sharding_key = normalize_ch_string(ch_sharding_key, "ch_sharding_key")
    ch_only_shard = _normalize_only_shard(ch_only_shard)

    _validate_backend_options(
        target_backend=target_config.backend,
        gp_distributed_by_key=gp_distribution,
        partition_by=partition,
        order_by=order,
        ch_engine=ch_engine_name,
        ch_cluster=ch_cluster_name,
        ch_sharding_key=ch_sharding_key,
        ch_only_shard=ch_only_shard,
    )
    if trino_insert_chunk_size is not None and trino_insert_chunk_size <= 0:
        raise ValueError("trino_insert_chunk_size must be a positive integer.")
    retry_per_host_drops = target_config.backend == "ch" and bool(
        ch_retry_per_host_drops
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
    )

    if (
        options.source_backend == "ch"
        and options.target_backend == "ch"
        and options.source_key == options.target_key
    ):
        return ch_create_table_as(
            options.target_key,
            options.target_table,
            options.source_sql,
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
        )

    if options.dry_run or options.return_sql:
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
            partition_by=options.partition_by,
            order_by=options.order_by,
            ch_engine=options.ch_engine,
            ch_cluster=options.ch_cluster,
            ch_sharding_key=options.ch_sharding_key,
            ch_only_shard=options.ch_only_shard,
            query_label=options.query_label,
        )

    source_connection: Any | None = None
    target_connection: Any | None = None
    inserted_rows: int | None = None
    delegate_transfer = False
    operation_metadata = SqlOperationMetadata(query_label=options.query_label)

    try:
        with tracked_sql_operation(
            metadata=operation_metadata,
            operation_name="create_table_from_sql",
            alias=options.target_key,
            backend=options.target_backend,
            phase="create_or_insert",
            query_label=options.query_label,
            preview_sql=options.source_sql,
        ):
            source_connection = get_sql_connection(source_config.connection_key)
            target_connection = (
                source_connection
                if source_config.connection_key == target_config.connection_key
                else get_sql_connection(target_config.connection_key)
            )

            time_print(
                "Inspecting source query schema",
                connection=source_config.connection_key,
                backend=source_config.backend,
            )
            source_schema = inspect_source_query_schema(
                source_config.backend,
                source_connection,
                apply_query_label(source_sql, query_label),
            )
            source_columns = [column.name for column in source_schema]
            _validate_source_columns(source_columns)
            validate_key_columns_in_columns(gp_distribution, source_columns)
            validate_ch_columns_in_columns(
                partition,
                source_columns,
                "partition_by",
                data_name="source query",
            )
            validate_ch_columns_in_columns(
                order,
                source_columns,
                "order_by",
                data_name="source query",
            )

            if options.table_schema is None:
                target_column_types = map_source_schema_to_target(
                    source_schema,
                    target_config.backend,
                )
            else:
                target_column_types = validate_table_schema_columns(
                    options.table_schema,
                    source_columns,
                )
            target_adapter = get_backend_adapter(target_config.backend)
            target_exists_before_drop = (
                target_adapter.prepare_existing_target_for_create_from_sql(
                    target_connection,
                    target_table,
                    drop_target_if_exists=drop_target_if_exists,
                    ch_cluster=ch_cluster_name,
                    ch_only_shard=options.ch_only_shard,
                    query_label=query_label,
                    connection_key=target_config.connection_key,
                    ch_retry_per_host_drops=options.ch_retry_per_host_drops,
                )
            )

            create_kwargs: dict[str, object] = {
                "table_schema": target_column_types,
                "gp_distributed_by_key": gp_distribution,
                "ch_engine": ch_engine_name,
                "ch_cluster": ch_cluster_name,
                "ch_sharding_key": ch_sharding_key,
                "ch_distributed_table": (
                    target_config.backend == "ch" and not options.ch_only_shard
                ),
                "ch_only_shard": options.ch_only_shard,
                "ch_replace_table": (
                    target_config.backend == "ch"
                    and not options.ch_only_shard
                    and drop_target_if_exists
                    and target_exists_before_drop
                ),
                "query_label": query_label,
            }
            if partition is not None:
                create_kwargs["partition_by"] = partition
            if order is not None:
                create_kwargs["order_by"] = order

            _create_sql_table_with_connection(
                target_config.backend,
                target_connection,
                target_table,
                None,
                connection_key=target_config.connection_key,
                **create_kwargs,
            )

            if not insert_data:
                if return_metadata:
                    return SqlOperationResult(
                        rows=None,
                        metadata=operation_metadata,
                    )
                return None

            if source_config.backend == target_config.backend:
                inserted_rows = insert_from_query(
                    target_config.backend,
                    target_connection,
                    target_table,
                    source_sql,
                    target_column_types,
                    query_label=query_label,
                )
            else:
                delegate_transfer = True
    except Exception as exc:
        annotate_sql_exception(
            exc,
            SqlOperationContext(
                operation="create_table_from_sql",
                alias=target_config.connection_key,
                backend=target_config.backend,
                phase="create_or_insert",
                target_table=target_table,
                sql_preview=sql_preview(source_sql),
            ),
        )
        raise
    finally:
        _close_connections(
            source_connection=source_connection,
            source_key=source_config.connection_key,
            source_backend=source_config.backend,
            target_connection=target_connection,
            target_key=target_config.connection_key,
            target_backend=target_config.backend,
        )

    if delegate_transfer:
        transfer_kwargs: dict[str, object] = {
            "from_db": source_config.connection_key,
            "to_db": target_config.connection_key,
            "from_sql": source_sql,
            "to_table": target_table,
            "write_mode": "append",
            "gp_distributed_by_key": gp_distribution,
            "trino_insert_chunk_size": trino_insert_chunk_size,
            "partition_by": partition,
            "order_by": order,
            "ch_engine": ch_engine_name,
            "ch_cluster": ch_cluster_name,
            "ch_sharding_key": ch_sharding_key,
        }
        if options.ch_only_shard:
            transfer_kwargs["ch_only_shard"] = True
        if query_label is not None:
            transfer_kwargs["query_label"] = query_label
        if options.table_schema is not None:
            transfer_kwargs["table_schema"] = target_column_types
        if return_metadata:
            transfer_kwargs["return_metadata"] = return_metadata
        return transfer_table(**transfer_kwargs)
    if return_metadata:
        operation_metadata.source_rows = inserted_rows
        operation_metadata.inserted_rows = inserted_rows
        operation_metadata.affected_rows = inserted_rows
        return SqlOperationResult(
            rows=inserted_rows,
            metadata=operation_metadata,
        )
    return inserted_rows


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
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_only_shard: bool,
    query_label: str | None,
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
            "partition_by": partition_by,
            "order_by": order_by,
            "ch_only_shard": ch_only_shard,
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
        add_create_table_steps(
            plan,
            _build_create_table_sqls(
                target_backend,
                target_table,
                pd.DataFrame(columns=list(table_schema)),
                table_schema=table_schema,
                gp_distributed_by_key=gp_distributed_by_key,
                partition_by=partition_by,
                order_by=order_by,
                ch_engine=ch_engine,
                ch_cluster=ch_cluster,
                ch_sharding_key=ch_sharding_key,
                ch_distributed_table=target_backend == "ch" and not ch_only_shard,
                ch_only_shard=ch_only_shard,
                query_label=query_label,
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
        raise InvalidSqlInputError(
            "create_table_from_sql expects exactly one SQL statement."
        )
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
) -> None:
    if gp_distributed_by_key and target_backend != "gp":
        raise ValueError(
            "gp_distributed_by_key can only be used when table_db has type 'gp'."
        )
    validate_ch_options_not_used(
        target_backend=target_backend,
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
        time_print(
            "Closing connection",
            connection=target_key,
            backend=target_backend,
            phase="close",
        )
        target_connection.close()
    if source_connection is not None:
        time_print(
            "Closing connection",
            connection=source_key,
            backend=source_backend,
            phase="close",
        )
        source_connection.close()
