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
    if not ch_distributed_table or ch_only_shard:
        return None

    expected: dict[str, str] = {}
    for column_name in batch.columns:
        column_key = str(column_name)
        expected[column_key] = (
            _explicit_column_type(column_types, column_key)
            if column_types is not None
            else adapter.infer_dataframe_column_type(batch[column_name])
        )
    return expected


def _explicit_column_type(
    column_types: dict[str, str],
    column_name: str,
) -> str:
    try:
        db_type = column_types[column_name]
    except KeyError as exc:
        raise ValueError(f"Missing explicit SQL type for column {column_name!r}.") from exc
    normalized = db_type.strip()
    if not normalized:
        raise ValueError(f"SQL type for column {column_name!r} must not be empty.")
    return normalized


def build_load_target_create_kwargs(
    adapter: Any,
    *,
    gp_distributed_by_key: list[str] | None,
    gp_partitions: Any = None,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_only_shard: bool,
    write_mode: str,
    original_target_exists: bool,
) -> dict[str, Any]:
    del adapter, gp_partitions
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
    gp_partitions: Any = None,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_only_shard: bool,
    drop_target_if_exists: bool,
    target_exists_before_drop: bool,
) -> dict[str, Any]:
    del adapter, gp_partitions
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
