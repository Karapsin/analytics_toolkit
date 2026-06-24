from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping


WriteMode = Literal["append", "replace", "truncate_insert", "upsert"]
BackendName = str


@dataclass(frozen=True)
class SourceColumn:
    name: str
    native_type: str | None = None
    precision: int | None = None
    scale: int | None = None


@dataclass(frozen=True)
class TargetWriteModeRequest:
    connection: Any
    table_name: str
    write_mode: str
    target_exists: bool
    replace_existing_non_ch: str
    ch_cluster: str = "{cluster}"
    connection_label: str | None = None
    drop_missing_ch_truncate_target: bool = True
    query_label: str | None = None
    connection_key: str | None = None
    ch_retry_per_host_drops: bool = True
    ch_only_shard: bool = False


@dataclass(frozen=True)
class StageTargetTableRequest:
    connection: Any
    target_table: str
    sample_batch: Any
    target_column_types: Mapping[str, str] | None
    gp_distributed_by_key: list[str] | None
    partition_by: list[str] | str | None
    order_by: list[str] | str | None
    ch_engine: str
    ch_cluster: str
    ch_sharding_key: str
    query_label: str | None
    connection_key: str | None
    ch_only_shard: bool = False


@dataclass(frozen=True)
class StageFinalizationRequest:
    connection: Any
    stage_table: str
    target_table: str
    replace_target_table: bool
    target_exists: bool
    sample_batch: Any
    target_column_types: Mapping[str, str] | None = None
    insert_column_types: Mapping[str, str] | None = None
    write_mode: str = "replace"
    key_columns: list[str] | None = None
    gp_distributed_by_key: list[str] | None = None
    partition_by: list[str] | str | None = None
    order_by: list[str] | str | None = None
    ch_engine: str = "ReplicatedMergeTree"
    ch_cluster: str = "{cluster}"
    ch_sharding_key: str = "rand()"
    query_label: str | None = None
    connection_key: str | None = None
    ch_retry_per_host_drops: bool = True
    ch_only_shard: bool = False
    upsert_partition_column: str | None = None
    final_upsert_stage_table: str | None = None
    incoming_stage_tables: list[str] | None = None
    trino_upsert_partition_drop_sql_template: str | None = None


@dataclass(frozen=True)
class BackendCapability:
    name: BackendName
    display_name: str
    sqlglot_dialect: str
    identifier_quote: str
    supports_transactions: bool
    supports_analyze: bool
    uses_stage_tables: bool
    supports_distributed_tables: bool
    truncate_semantics: str
    drop_semantics: str
    create_semantics: str
    type_family: str
    supported_write_modes: frozenset[WriteMode]
    supports_early_transfer_target_creation: bool = True
    upsert_strategy: str = "key_delete_insert"
    requires_upsert_partition_column: bool = False
    requires_upsert_partition_drop_template: bool = False
    supports_show_tables_catalog_filter: bool = False
