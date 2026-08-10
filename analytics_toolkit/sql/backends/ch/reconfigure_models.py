from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from analytics_toolkit.sql.connection.ddl_defaults import ClickHouseScopeDefaults
    from analytics_toolkit.sql.execution.plans import SqlPlan


@dataclass(frozen=True)
class ChReconfigureOptions:
    connection_key: str
    table: str
    ch_engine: str | None = None
    partition_by: Sequence[str] | str | None = None
    order_by: Sequence[str] | str | None = None
    ch_sharding_key: str | None = None
    ch_distributed_table: bool | None = None
    ch_distributed_engine_template: str | None = None
    ch_distributed_cluster: str | None = None
    ch_shard_on_cluster: str | None = None
    ch_distributed_on_cluster: str | None = None
    ch_settings: Mapping[str, str | int | float | bool | None] | None = None
    reset_partition_by: bool = False
    reset_order_by: bool = False
    to_defaults: bool = False
    regular_defaults: ClickHouseScopeDefaults | None = None
    validate_row_count: bool = True
    query_label: str | None = None
    ch_ddl_wait_policy: str = "wait_all"


@dataclass
class ChReconfiguration:
    plan: SqlPlan
    strategy: str
    table: str
    source_table: str
    replacement_table: str | None
    source_cluster: str | None
    target_cluster: str | None
    source_cluster_resolved: str | None
    target_cluster_resolved: str | None
    source_pair: bool
    target_pair: bool
    distributed_on_cluster: str | None
    distributed_cluster: str | None
    database_engine: str
    before_ddl: dict[str, str]
    after_ddl: dict[str, str]
    temporary_tables: list[str] = field(default_factory=list)
    temporary_table_scopes: list[tuple[str, str | None]] = field(default_factory=list)
    temporary_table_roles: list[tuple[str, str | None, str]] = field(default_factory=list)
    backup_tables: list[str] = field(default_factory=list)
    cleanup_tables: list[tuple[str, str | None]] = field(default_factory=list)
    rollback_sqls: list[str] = field(default_factory=list)
    cutover_sqls: list[str] = field(default_factory=list)
    final_count_cluster: str | None = None
    source_count: int | None = None
    replacement_count: int | None = None
    final_count: int | None = None
    cleanup_complete: bool = False
    cleanup_error: str | None = None

    def result_data(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "source_cluster": self.source_cluster,
            "target_cluster": self.target_cluster,
            "source_cluster_resolved": self.source_cluster_resolved,
            "target_cluster_resolved": self.target_cluster_resolved,
            "source_pair": self.source_pair,
            "target_pair": self.target_pair,
            "distributed_on_cluster": self.distributed_on_cluster,
            "distributed_cluster": self.distributed_cluster,
            "database_engine": self.database_engine,
            "before_ddl": dict(self.before_ddl),
            "after_ddl": dict(self.after_ddl),
            "temporary_tables": list(self.temporary_tables),
            "backup_tables": list(self.backup_tables),
            "source_count": self.source_count,
            "replacement_count": self.replacement_count,
            "final_count": self.final_count,
            "row_count_validated": self.plan.metadata.row_count_validated,
            "cleanup_complete": self.cleanup_complete,
            "cleanup_error": self.cleanup_error,
        }


__all__ = ["ChReconfiguration", "ChReconfigureOptions"]
