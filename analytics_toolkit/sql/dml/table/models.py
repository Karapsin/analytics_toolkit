from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class CreateTableFromSqlOptions:
    source_key: str
    source_backend: str
    target_key: str
    target_backend: str
    target_table: str
    source_sql: str
    table_schema: dict[str, str] | None = None
    insert_data: bool = True
    drop_target_if_exists: bool = False
    gp_distributed_by_key: list[str] | None = None
    gp_partitions: Any = None
    partition_by: Sequence[str] | str | None = None
    order_by: Sequence[str] | str | None = None
    ch_engine: str = "ReplicatedMergeTree"
    ch_cluster: str = "{cluster}"
    ch_sharding_key: str = "rand()"
    ch_only_shard: bool = False
    ch_retry_per_host_drops: bool = True
    trino_insert_chunk_size: int | None = None
    dry_run: bool = False
    return_sql: bool = False
    return_metadata: bool = False
    query_label: str | None = None
    ddl_properties: Mapping[str, Any] | None = None
    ch_creation_policy: Any = None


@dataclass(frozen=True)
class DropManyPartitionsOptions:
    connection_key: str
    backend: str
    target_table: str
    partition_keys: list[str]
    trino_partition_column: str | None = None
    gp_truncate: bool = False
    ch_cluster: str = "{cluster}"
    retry_cnt: int = 5
    timeout_increment: int | float = 5
    dry_run: bool = False
    return_sql: bool = False
    return_metadata: bool = False
    query_label: str | None = None


@dataclass(frozen=True)
class ChCreateTableAsOptions:
    connection_key: str
    backend: str
    target_table: str
    query_sql: str
    table_schema: dict[str, str] | None = None
    insert_data: bool = True
    drop_target_if_exists: bool = True
    partition_by: Sequence[str] | str | None = None
    order_by: Sequence[str] | str | None = None
    ch_engine: str = "ReplicatedMergeTree"
    ch_cluster: str = "{cluster}"
    ch_sharding_key: str = "rand()"
    ch_only_shard: bool = False
    ch_retry_per_host_drops: bool = True
    dry_run: bool = False
    return_sql: bool = False
    return_metadata: bool = False
    query_label: str | None = None


@dataclass(frozen=True)
class ChDropTableOptions:
    connection_key: str
    backend: str
    target_table: str
    ch_shard_table: str
    ch_only_shard: bool = False
    ch_cluster: str | None = "{cluster}"
    if_exists: bool = True
    ch_wait_for_absence: bool = False
    ch_wait_timeout_seconds: int = 300
    ch_wait_poll_interval_seconds: float = 1
    ch_retry_per_host_drops: bool = True
    dry_run: bool = False
    return_sql: bool = False
    return_metadata: bool = False
    query_label: str | None = None
