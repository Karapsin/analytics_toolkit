from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def should_ensure_load_target_table(adapter: Any, target_exists: bool) -> bool:
    del adapter, target_exists
    return True


def expected_create_table_column_types(
    adapter: Any,
    batch: Any,
    column_types: dict[str, str] | None,
    *,
    ch_distributed_table: bool,
    ch_only_shard: bool,
) -> dict[str, str] | None:
    del adapter
    if not ch_distributed_table or ch_only_shard:
        return None

    from ...ddl.schema import _build_expected_ch_column_types

    return _build_expected_ch_column_types(batch, column_types)


def build_load_target_create_kwargs(
    adapter: Any,
    *,
    gp_distributed_by_key: list[str] | None,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_only_shard: bool,
    write_mode: str,
    original_target_exists: bool,
) -> dict[str, Any]:
    del adapter
    return {
        "gp_distributed_by_key": gp_distributed_by_key,
        "partition_by": partition_by,
        "order_by": order_by,
        "ch_engine": ch_engine,
        "ch_cluster": ch_cluster,
        "ch_sharding_key": ch_sharding_key,
        "ch_distributed_table": not ch_only_shard,
        "ch_only_shard": ch_only_shard,
        "ch_replace_table": (
            write_mode == "replace" and original_target_exists and not ch_only_shard
        ),
    }


def build_create_from_sql_target_create_kwargs(
    adapter: Any,
    *,
    gp_distributed_by_key: list[str] | None,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_only_shard: bool,
    drop_target_if_exists: bool,
    target_exists_before_drop: bool,
) -> dict[str, Any]:
    del adapter
    return {
        "gp_distributed_by_key": gp_distributed_by_key,
        "partition_by": partition_by,
        "order_by": order_by,
        "ch_engine": ch_engine,
        "ch_cluster": ch_cluster,
        "ch_sharding_key": ch_sharding_key,
        "ch_distributed_table": not ch_only_shard,
        "ch_only_shard": ch_only_shard,
        "ch_replace_table": (
            not ch_only_shard and drop_target_if_exists and target_exists_before_drop
        ),
    }
