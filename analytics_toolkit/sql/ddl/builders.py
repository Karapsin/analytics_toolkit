from __future__ import annotations

from collections.abc import Sequence

from ..backends import get_backend
from ..connection.errors import UnsupportedConnectionTypeError
from ..execution.labels import apply_query_label


def _validate_only_shard(backend: str, ch_only_shard: bool, option_owner: str) -> None:
    if not isinstance(ch_only_shard, bool):
        raise ValueError("ch_only_shard must be a boolean.")
    if ch_only_shard and backend != "ch":
        raise ValueError(
            f"ch_only_shard can only be used when {option_owner} has type 'ch'."
        )


def _build_backend_create_table_sqls(
    *,
    backend: str,
    table_name: str,
    joined_columns: str,
    gp_distributed_by_key: list[str] | None,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_distributed_table: bool,
    ch_only_shard: bool,
    ch_replace_table: bool,
) -> list[str]:
    try:
        backend_adapter = get_backend(backend)
    except UnsupportedConnectionTypeError as exc:
        raise UnsupportedConnectionTypeError(
            "Unsupported connection type. Expected one of: 'trino', 'gp', 'ch'."
        ) from exc
    return backend_adapter.build_create_table_sqls(
        table_name=table_name,
        joined_columns=joined_columns,
        gp_distributed_by_key=gp_distributed_by_key,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_distributed_table=ch_distributed_table,
        ch_only_shard=ch_only_shard,
        ch_replace_table=ch_replace_table,
    )


def _apply_query_label_to_sqls(sqls: list[str], query_label: str | None) -> list[str]:
    return [apply_query_label(sql, query_label) for sql in sqls]
