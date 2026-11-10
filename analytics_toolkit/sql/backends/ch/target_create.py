from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext
from typing import Any

from .routing import local_sql


def should_ensure_load_target_table(
    adapter: Any, target_exists: bool, *, connection: Any = None
) -> bool:
    del adapter
    return not target_exists or not getattr(connection, "_ch_outage_client", False)


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


def local_creation_context(adapter: Any, connection: Any, *, local: bool) -> Any:
    del adapter
    return local_sql(connection) if local else nullcontext()


def apply_target_write_mode(adapter: Any, request: Any) -> bool:
    from .layout import inspect_existing_layout  # noqa: PLC0415 - adapter initialization cycle
    from .lifecycle import truncate_ch_distributed_table_pair  # noqa: PLC0415 - adapter cycle
    from .operations import drop_table_with_options  # noqa: PLC0415 - adapter cycle

    if request.write_mode in {"append", "upsert"} or not request.target_exists:
        return bool(request.target_exists)
    layout = inspect_existing_layout(
        request.connection, request.table_name, distributed_on_cluster=request.ch_cluster
    )
    if request.write_mode == "truncate_insert":
        if layout.create_distributed_pair:
            truncate_ch_distributed_table_pair(
                request.connection,
                request.table_name,
                shard_table=layout.physical_table,
                ch_cluster=layout.shard_on_cluster,
                query_label=request.query_label,
            )
        else:
            adapter.clear_table(
                request.connection, request.table_name, query_label=request.query_label
            )
        return True
    drop_table_with_options(
        adapter,
        request.connection,
        request.table_name,
        connection_key=request.connection_key or "ch",
        ch_cluster=request.ch_cluster,
        ch_wait_for_absence=True,
        ch_retry_per_host_drops=request.ch_retry_per_host_drops,
        if_exists=True,
        query_label=request.query_label,
    )
    return False


def preflight_create_table_replacement(adapter: Any, connection: Any, options: Any) -> None:
    from .creation_preflight import (  # noqa: PLC0415 - adapter cycle
        preflight_create_table_replacement as preflight,
    )

    preflight(adapter, connection, options)


def build_create_table_placeholder_sql(
    adapter: Any,
    table_name: str,
    *,
    replace_table: bool = False,
) -> str:
    del adapter
    statement = "CREATE OR REPLACE TABLE" if replace_table else "CREATE TABLE"
    return f"{statement} {table_name} (<source query schema>)"
