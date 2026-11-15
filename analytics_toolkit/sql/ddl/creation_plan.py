"""Creation plans retain unresolved destructive resource checks explicitly."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.execution.plans import SqlOperationMetadata, SqlPlan

from .drop_planning import apply_pending_creation_drop

if TYPE_CHECKING:
    from collections.abc import Callable

    from .models import CreateSqlTableOptions


def build_create_table_plan(
    options: CreateSqlTableOptions,
    drop_sqls: list[str],
    create_sqls: list[str],
    *,
    gp_partition_option: Callable[[Any], Any],
) -> tuple[SqlOperationMetadata, SqlPlan]:
    metadata = SqlOperationMetadata(
        statement_count=len(drop_sqls) + len(create_sqls),
        query_label=options.query_label,
    )
    plan = SqlPlan(
        operation="create_table",
        target_alias=options.connection_key,
        target_backend=options.backend,
        target_table=options.table_name,
        options={
            "drop_if_exists": options.drop_target_if_exists,
            "drop_target_if_exists": options.drop_target_if_exists,
            "if_not_exists": options.if_not_exists,
            "pending_existing_layout_verification": (
                options.drop_target_if_exists or options.ch_replace_table
            ),
            "gp_partitions": gp_partition_option(options.gp_partitions),
            "ch_ddl_wait_policy": (
                options.ch_creation_policy.ddl_wait_policy
                if options.ch_creation_policy is not None
                else None
            ),
        },
        metadata=metadata,
    )
    plan.extend(
        drop_sqls,
        alias=options.connection_key,
        backend=options.backend,
        phase="drop_target",
        target_table=options.table_name,
    )
    plan.extend(
        create_sqls,
        alias=options.connection_key,
        backend=options.backend,
        phase="create_table",
        target_table=options.table_name,
    )
    apply_pending_creation_drop(
        plan,
        get_backend_adapter(options.backend),
        requested=options.drop_target_if_exists or options.ch_replace_table,
    )
    return metadata, plan


__all__ = ["build_create_table_plan"]
