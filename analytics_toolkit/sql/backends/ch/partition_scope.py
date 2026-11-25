"""Resolve partition mutations from verified existing resources before execution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.execution.labels import apply_query_label

if TYPE_CHECKING:
    from analytics_toolkit.sql.dml.table.models import DropManyPartitionsOptions
    from analytics_toolkit.sql.execution.plans import SqlPlan


def prepare_drop_partitions_plan(
    adapter: Any, options: DropManyPartitionsOptions, plan: SqlPlan
) -> None:
    del adapter, options
    plan.statements.clear()
    plan.metadata.statement_count = 0
    plan.options.update(
        layout_resolution_pending=True,
        ownership_resolution_pending=True,
        execution_requires=["verified existing layout", "exact partition execution scope"],
    )


def execute_drop_partitions(
    adapter: Any, connection: Any, options: DropManyPartitionsOptions, plan: SqlPlan
) -> None:
    # Adapter registry initialization cycle.
    from analytics_toolkit.sql.connection.config import (  # noqa: PLC0415
        ChConfig,
        get_connection_config,
    )

    from .drop_layout import (  # noqa: PLC0415 - adapter registry initialization cycle.
        ResourceInspection,
        VerifiedDropLayout,
        inspect_resource,
        require_drop_configuration,
        require_drop_resource,
    )

    # Adapter registry initialization cycle.
    from .layout import inspect_existing_layout  # noqa: PLC0415

    # Adapter registry initialization cycle.
    from .partition_literals import render_partition_value  # noqa: PLC0415

    # Adapter registry initialization cycle.
    from .reconfigure_execution import cluster_clause  # noqa: PLC0415
    from .routing import local_sql  # noqa: PLC0415 - adapter registry initialization cycle.

    layout = inspect_existing_layout(connection, options.target_table)
    if layout.create_distributed_pair and not layout.owns_physical:
        message = "Partition removal cannot mutate externally owned ClickHouse storage."
        raise InvalidSqlInputError(message)
    target = layout.physical_table
    cluster = layout.shard_on_cluster
    sqls = tuple(
        apply_query_label(
            f"ALTER TABLE {target}{cluster_clause(cluster)} DROP PARTITION "
            f"{render_partition_value(value)}",
            options.query_label,
        )
        for value in options.partition_keys
    )
    resource = inspect_resource(
        connection, target, cluster, "partition", ResourceInspection(sqls=sqls)
    )
    if resource is None:
        message = "The selected ClickHouse partition target disappeared before mutation."
        raise InvalidSqlInputError(message)
    config = get_connection_config(options.connection_key)
    if not isinstance(config, ChConfig):
        message = "ClickHouse partition removal requires a ClickHouse connection."
        raise InvalidSqlInputError(message)
    certificate = VerifiedDropLayout((resource,), config)
    plan.statements.clear()
    for sql in sqls:
        plan.add(
            sql,
            alias=options.connection_key,
            backend="ch",
            phase="drop_partitions",
            target_table=target,
        )
    plan.metadata.statement_count = len(sqls)
    plan.options.update(layout_resolution_pending=False, ownership_resolution_pending=False)
    plan.options.pop("execution_requires", None)
    for sql in sqls:
        require_drop_configuration(certificate)
        require_drop_resource(connection, resource, sqls=sqls)
        with local_sql(connection):
            adapter.execute_command(connection, sql)
