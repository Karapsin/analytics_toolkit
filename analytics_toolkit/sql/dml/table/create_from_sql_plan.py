from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.ddl.api import _build_create_table_sqls, _gp_partition_plan_option
from analytics_toolkit.sql.execution.plan_steps import (
    add_create_table_placeholder_step,
    add_create_table_steps,
    add_drop_target_steps,
    add_insert_query_step,
    add_inspect_schema_step,
)
from analytics_toolkit.sql.execution.plans import SqlPlan

if TYPE_CHECKING:
    from collections.abc import Sequence


def _build_create_table_from_sql_plan(  # noqa: PLR0913
    *,
    source_key: str,
    source_backend: str,
    target_key: str,
    target_backend: str,
    target_table: str,
    source_sql: str,
    table_schema: dict[str, str] | None,
    insert_data: bool,
    drop_target_if_exists: bool,
    gp_distributed_by_key: list[str] | None,
    gp_partitions: Any,
    partition_by: Sequence[str] | str | None,
    order_by: Sequence[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_only_shard: bool,
    query_label: str | None,
    ddl_properties: dict[str, Any] | None,
    ch_creation_policy: Any,
) -> SqlPlan:
    plan = SqlPlan(
        operation="create_table_from_sql",
        source_alias=source_key,
        target_alias=target_key,
        source_backend=source_backend,
        target_backend=target_backend,
        target_table=target_table,
        options={
            "insert_data": insert_data,
            "drop_target_if_exists": drop_target_if_exists,
            "table_schema": table_schema,
            "gp_distributed_by_key": gp_distributed_by_key,
            "gp_partitions": _gp_partition_plan_option(gp_partitions),
            "partition_by": partition_by,
            "order_by": order_by,
            "ch_only_shard": ch_only_shard,
            "ch_ddl_wait_policy": (
                ch_creation_policy.ddl_wait_policy if ch_creation_policy is not None else None
            ),
        },
    )
    add_inspect_schema_step(
        plan,
        alias=source_key,
        backend=source_backend,
        source_sql=source_sql,
        query_label=query_label,
    )
    if drop_target_if_exists:
        add_drop_target_steps(
            plan,
            alias=target_key,
            backend=target_backend,
            table_name=target_table,
            ch_cluster=ch_cluster,
            query_label=query_label,
            ch_only_shard=ch_only_shard,
        )
    if table_schema is None:
        add_create_table_placeholder_step(
            plan,
            alias=target_key,
            backend=target_backend,
            table_name=target_table,
            query_label=query_label,
        )
    else:
        create_kwargs = get_backend_adapter(
            target_backend
        ).build_create_from_sql_target_create_kwargs(
            gp_distributed_by_key=gp_distributed_by_key,
            gp_partitions=gp_partitions,
            partition_by=partition_by,
            order_by=order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_only_shard=ch_only_shard,
            drop_target_if_exists=drop_target_if_exists,
            target_exists_before_drop=False,
        )
        add_create_table_steps(
            plan,
            _build_create_table_sqls(
                target_backend,
                target_table,
                pd.DataFrame(columns=list(table_schema)),
                table_schema=table_schema,
                query_label=query_label,
                ddl_properties=ddl_properties,
                ch_creation_policy=ch_creation_policy,
                **create_kwargs,
            ),
            alias=target_key,
            backend=target_backend,
            table_name=target_table,
        )
    if insert_data:
        add_insert_query_step(
            plan,
            alias=target_key,
            backend=target_backend,
            target_table=target_table,
            source_sql=source_sql,
            phase="insert_data",
            query_label=query_label,
        )
    return plan
