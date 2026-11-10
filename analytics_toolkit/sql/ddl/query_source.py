from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd

from analytics_toolkit.sql.connection.errors import SqlOperationContext, sql_preview
from analytics_toolkit.sql.core.type_resolution import requires_adaptive_resolution
from analytics_toolkit.sql.execution.labels import apply_query_label
from analytics_toolkit.sql.execution.operation_runner import (
    _close_connection,
    run_retrying_operation,
)

from .ch_policy import regular_ddl_properties

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


def generate_create_table_from_query_sql(  # noqa: PLR0913 - Mirrors the public create_table options.
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
    drop_target_if_exists: bool,
    query_label: str | None,
    retry_cnt: int,
    timeout_increment: float,
    get_connection_config_fn: Callable[[str], Any],
    get_backend_adapter_fn: Callable[[str], Any],
    get_sql_connection_fn: Callable[[str], Any],
    ch_creation_policy: Any = None,
    ch_replace_table: bool = False,
) -> str:
    # Source creation imports the DDL facade; defer these shared normalizers until execution.
    # The API calls this helper; import its compatibility builders after initialization.
    from analytics_toolkit.sql.ddl.api import (  # noqa: PLC0415
        _build_create_table_sqls,
        _format_sql_statements,
    )
    from analytics_toolkit.sql.dml.table.create_table_from_sql import (  # noqa: PLC0415
        _normalize_only_shard,
        _normalize_single_query,
        _validate_source_columns,
    )
    from analytics_toolkit.sql.dml.table.table_validation import (  # noqa: PLC0415
        normalize_key_columns,
        validate_key_columns_in_columns,
    )
    from analytics_toolkit.sql.dml.transfer.schema import (  # noqa: PLC0415
        inspect_source_query_schema,
        map_source_schema_to_target,
    )

    source_config = get_connection_config_fn(source_db)
    target_config = get_connection_config_fn(table_db)
    target_adapter = get_backend_adapter_fn(target_config.backend)
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
        source_connection = get_sql_connection_fn(source_config.connection_key)
        try:
            return inspect_source_query_schema(
                source_config.backend,
                source_connection,
                apply_query_label(source_sql, query_label),
            )
        finally:
            _close_connection(
                {"connection": source_connection},
                source_config.connection_key,
                backend=source_config.backend,
            )

    source_schema = run_retrying_operation(
        operation_name=(
            f"inspecting query schema on {source_config.connection_key} ({source_config.backend})"
        ),
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        operation=inspect_schema,
        context_factory=lambda attempt: SqlOperationContext(
            operation="create_table",
            alias=source_config.connection_key,
            backend=source_config.backend,
            phase="inspect_schema",
            target_table=table_name,
            retry_attempt=attempt,
            sql_preview=sql_preview(source_sql),
        ),
    )

    if requires_adaptive_resolution(source_schema, target_config.backend, source_config.backend):
        message = (
            "SQL generation requires adaptive full-input profiling; execute the operation "
            "to resolve exact types or provide an explicit table_schema."
        )
        raise ValueError(message)
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
        drop_target_if_exists=drop_target_if_exists,
        target_exists_before_drop=False,
    )
    create_kwargs["ch_replace_table"] = ch_replace_table
    create_sqls = _build_create_table_sqls(
        target_config.backend,
        table_name,
        pd.DataFrame(columns=source_columns),
        table_schema=target_column_types,
        query_label=query_label,
        option_owner="db_key",
        ddl_properties=regular_ddl_properties(target_config),
        ch_creation_policy=ch_creation_policy,
        **create_kwargs,
    )
    drop_sqls = (
        target_adapter.build_drop_target_sqls(
            table_name,
            ch_cluster=ch_cluster_name,
            ch_only_shard=only_shard,
            query_label=query_label,
        )
        if drop_target_if_exists
        else []
    )
    return _format_sql_statements([*drop_sqls, *create_sqls])


__all__ = ["generate_create_table_from_query_sql"]
