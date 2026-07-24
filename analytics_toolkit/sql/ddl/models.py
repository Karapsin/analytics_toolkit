from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd


@dataclass(frozen=True)
class CreateSqlTableOptions:
    connection_key: str
    backend: str
    table_name: str
    df: pd.DataFrame
    table_schema: dict[str, str] | None = None
    gp_distributed_by_key: list[str] | None = None
    gp_partitions: Any = None
    partition_by: Sequence[str] | str | None = None
    order_by: Sequence[str] | str | None = None
    ch_engine: str = "ReplicatedMergeTree"
    ch_cluster: str = "{cluster}"
    ch_sharding_key: str = "rand()"
    ch_distributed_table: bool = False
    ch_only_shard: bool = False
    ch_replace_table: bool = False
    dry_run: bool = False
    return_sql: bool = False
    query_label: str | None = None
    return_metadata: bool = False
    ddl_properties: Mapping[str, Any] | None = None
    ch_creation_policy: Any = None
