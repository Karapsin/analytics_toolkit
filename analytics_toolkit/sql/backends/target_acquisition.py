"""Create an absent transfer target with explicit acknowledgement observers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import StageTargetTableRequest


def ensure_stage_target_table(adapter: Any, request: StageTargetTableRequest) -> bool:
    # DDL API and backend registry cycle.
    from analytics_toolkit.sql.ddl.api import _create_sql_table_with_connection  # noqa: PLC0415

    create_kwargs: dict[str, Any] = {}
    if request.partition_by is not None:
        create_kwargs["partition_by"] = request.partition_by
    if request.gp_partitions is not None:
        create_kwargs["gp_partitions"] = request.gp_partitions
    if request.order_by is not None:
        create_kwargs["order_by"] = request.order_by
    if request.on_resource_created is not None or request.on_scoped_resource_created is not None:
        create_kwargs.update(
            on_resource_created=request.on_resource_created,
            on_scoped_resource_created=request.on_scoped_resource_created,
        )
    if adapter.backend == "ch":
        create_kwargs.update(
            ch_engine=request.ch_engine,
            ch_cluster=request.ch_cluster,
            ch_sharding_key=request.ch_sharding_key,
            ch_distributed_table=not request.ch_only_shard,
            ch_only_shard=request.ch_only_shard,
            ch_creation_policy=request.ch_creation_policy,
        )
    _create_sql_table_with_connection(
        adapter.backend,
        request.connection,
        request.target_table,
        None if request.target_column_types is not None else request.sample_batch,
        connection_key=request.connection_key or adapter.backend,
        table_schema=request.target_column_types,
        gp_distributed_by_key=request.gp_distributed_by_key,
        query_label=request.query_label,
        **create_kwargs,
    )
    return True
