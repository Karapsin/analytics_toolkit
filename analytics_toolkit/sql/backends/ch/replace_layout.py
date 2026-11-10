"""Convert a verified existing layout before applying a staged replacement."""

from __future__ import annotations

from typing import Any

from analytics_toolkit.sql.connection.ddl_defaults import (
    ClickHouseDistributedDefaults,
    ClickHouseObjectDefaults,
    ClickHouseScopeDefaults,
)
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError

from .layout import inspect_existing_layout
from .reconfigure import execute_ch_table_reconfiguration, plan_ch_table_reconfiguration
from .reconfigure_ddl import engine_sql, parse_create_table
from .reconfigure_models import ChReconfigureOptions


def prepare_replacement_layout(adapter: Any, request: Any) -> None:
    policy = request.ch_creation_policy
    if policy is None:
        return
    layout = inspect_existing_layout(
        request.connection,
        request.target_table,
        distributed_on_cluster=policy.distributed_on_cluster,
    )
    if not layout.owns_physical:
        message = (
            "The existing Distributed facade references external physical storage. "
            "Name that storage explicitly or establish managed ownership "
            "before structural replacement."
        )
        raise AmbiguousSqlReplaceError(message)
    desired_pair = policy.create_distributed_pair and not request.ch_only_shard
    if layout.create_distributed_pair == desired_pair:
        return
    # First preserve all existing data in the requested topology. The guarded
    # replacement subsequently applies the new schema and contents. Using the
    # old engine/order and rand() here avoids referencing new-only columns while
    # copying the old rows. The final facade uses the caller's actual template.
    source_engine = engine_sql(parse_create_table(layout.physical_ddl, layout.physical_table))
    defaults = ClickHouseScopeDefaults(
        create_distributed_pair=desired_pair,
        shard=ClickHouseObjectDefaults(source_engine, policy.shard_on_cluster),
        distributed=ClickHouseDistributedDefaults(
            policy.distributed_engine_template,
            policy.distributed_cluster,
            policy.distributed_on_cluster,
            "rand()",
        ),
    )
    options = ChReconfigureOptions(
        connection_key=request.connection_key or "ch",
        table=request.target_table,
        ch_distributed_table=desired_pair,
        ch_distributed_on_cluster=layout.distributed_on_cluster if not desired_pair else None,
        to_defaults=True,
        regular_defaults=defaults,
        ch_ddl_wait_policy=policy.ddl_wait_policy,
        query_label=request.query_label,
    )
    change = plan_ch_table_reconfiguration(adapter, request.connection, options)
    execute_ch_table_reconfiguration(adapter, request.connection, change, validate_row_count=True)
    if not change.cleanup_complete:
        message = (
            "ClickHouse layout conversion retained recovery artifacts; reconcile them before "
            f"continuing replacement: {change.result_data()}"
        )
        raise AmbiguousSqlReplaceError(message)


__all__ = ["prepare_replacement_layout"]
