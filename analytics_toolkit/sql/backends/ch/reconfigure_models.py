from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from analytics_toolkit.sql.execution.plans import SqlPlan


@dataclass(frozen=True)
class ChReconfigureOptions:
    connection_key: str
    table: str
    ch_engine: str | None = None
    ch_partition_by: Sequence[str] | str | None = None
    ch_order_by: Sequence[str] | str | None = None
    ch_cluster: str | None = None
    ch_source_cluster: str | None = None
    ch_sharding_key: str | None = None
    ch_settings: Mapping[str, str | int | float | bool | None] | None = None
    ch_reset_partition_by: bool = False
    ch_reset_order_by: bool = False
    validate_row_count: bool = True
    query_label: str | None = None


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
    database_engine: str
    before_ddl: dict[str, str]
    after_ddl: dict[str, str]
    temporary_tables: list[str] = field(default_factory=list)
    backup_tables: list[str] = field(default_factory=list)
    cleanup_tables: list[tuple[str, str | None]] = field(default_factory=list)
    rollback_sqls: list[str] = field(default_factory=list)
    cutover_sqls: list[str] = field(default_factory=list)
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
