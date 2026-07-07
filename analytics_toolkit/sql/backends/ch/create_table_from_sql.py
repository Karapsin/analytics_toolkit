from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def create_table_from_sql_fast_path(
    adapter: Any,
    *,
    source_backend: str,
    source_key: str,
    target_key: str,
    target_table: str,
    source_sql: str,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_only_shard: bool,
    ch_retry_per_host_drops: bool,
    insert_data: bool,
    drop_target_if_exists: bool,
    dry_run: bool,
    return_sql: bool,
    query_label: str | None,
    return_metadata: bool,
    table_schema: dict[str, str] | None,
) -> tuple[bool, Any]:
    if source_backend != adapter.backend or source_key != target_key:
        return False, None

    from .create_table_as import ch_create_table_as

    result = ch_create_table_as(
        target_key,
        target_table,
        source_sql,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_only_shard=ch_only_shard,
        ch_retry_per_host_drops=ch_retry_per_host_drops,
        insert_data=insert_data,
        drop_target_if_exists=drop_target_if_exists,
        dry_run=dry_run,
        return_sql=return_sql,
        query_label=query_label,
        return_metadata=return_metadata,
        table_schema=table_schema,
    )
    return True, result
