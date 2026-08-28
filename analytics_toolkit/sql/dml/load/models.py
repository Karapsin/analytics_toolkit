from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from analytics_toolkit.sql.dml.empty_source import EmptySourcePolicy


@dataclass(frozen=True)
class LoadOptions:
    connection_key: str
    connection_backend: str
    destination_table: str
    destination_hash: str | None = None
    table_schema: dict[str, str] | None = None
    append: bool = False
    write_mode: str = "replace"
    empty_source_policy: EmptySourcePolicy | None = None
    gp_distributed_by_key: list[str] | None = None
    gp_partitions: Any = None
    key_columns: list[str] | None = None
    upsert_partition_column: str | None = None
    trino_upsert_partition_drop_sql_template: str | None = None
    trino_insert_chunk_size: int | None = None
    gp_insert_chunk_size: int | None = None
    partition_by: list[str] | str | None = None
    order_by: list[str] | str | None = None
    ch_engine: str = "ReplicatedMergeTree"
    ch_cluster: str = "{cluster}"
    ch_sharding_key: str = "rand()"
    ch_only_shard: bool = False
    ch_retry_per_host_drops: bool = True
    query_label: str | None = None
    transfer_staging_schema: str | None = None
    s3_transfer_staging_schema: str | None = None
    s3_transfer_staging_location: str | None = None
    parquet_storage_options: Mapping[str, Any] | None = field(default=None, repr=False)
    transfer_staging_username: str | None = None
    use_parquet_staging: bool = False
    retry_cnt: int = 5
    timeout_increment: int | float = 5
    regular_ddl_properties: Mapping[str, Any] | None = None
    staging_ddl_properties: Mapping[str, Any] | None = None
    parquet_ddl_properties: Mapping[str, Any] | None = None
    regular_ch_policy: Any = None
    staging_ch_policy: Any = None


@dataclass
class LoadState:
    target_exists: bool
    original_target_exists: bool
    target_created_by_operation: bool = False
    overlap_stage_table: str | None = None
    final_upsert_stage_table: str | None = None
    stage_external_location: str | None = None
    target_column_types: dict[str, str] | None = None
