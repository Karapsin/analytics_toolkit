"""Pass the resolved query-create policy through the optional backend fast path."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from .models import CreateTableFromSqlOptions


def call_create_table_from_sql_fast_path(
    *, target_adapter: Any, options: CreateTableFromSqlOptions
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
            ch_creation_policy=options.ch_creation_policy,
            ch_replace_table=options.ch_replace_table,
            input_resolution=options.input_resolution,
        ),
    )
