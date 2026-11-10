"""Acknowledged ownership of off-side replacement resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError
from analytics_toolkit.sql.ddl.schema import (
    _build_column_definitions,
    build_table_schema_column_definitions,
)
from analytics_toolkit.sql.execution.backend_options import current_backend_operation
from analytics_toolkit.sql.execution.labels import apply_query_label

from .creation_policy import build_policy_create_sqls
from .reconfigure_execution import execute_reconfiguration_sqls
from .routing import local_sql
from .wait import (
    _wait_for_ch_table,
    _wait_for_ch_table_on_cluster,
)
from .wait_policy import waits_for_shard

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class ReplacementCreate:
    sql: str
    table: str
    cluster: str | None


def submit_replacement_create(
    adapter: Any,
    request: Any,
    resource: ReplacementCreate,
    on_resource_created: Callable[[tuple[str, str | None]], None] | None = None,
) -> None:
    try:
        with local_sql(request.connection):
            execute_reconfiguration_sqls(adapter, request.connection, [resource.sql])
    except Exception as exc:
        state = current_backend_operation()
        if state is not None:
            state.preserve_recovery = True
            state.structural_replay_blocked = True
        message = (
            "ClickHouse replacement CREATE was not acknowledged; the candidate is retained "
            f"without claiming ownership: {resource.table}, scope={resource.cluster!r}. "
            f"Original failure: {exc}"
        )
        raise AmbiguousSqlReplaceError(message) from exc
    if on_resource_created is not None:
        on_resource_created((resource.table, resource.cluster))


def create_replacement_physical(
    adapter: Any,
    request: Any,
    replacement_physical: str,
    on_resource_created: Callable[[tuple[str, str | None]], None] | None = None,
) -> None:
    policy = request.ch_creation_policy
    columns = (
        build_table_schema_column_definitions("ch", request.target_column_types)
        if request.target_column_types is not None
        else _build_column_definitions("ch", request.sample_batch, None)
    )
    sql = build_policy_create_sqls(
        table_name=replacement_physical,
        joined_columns=columns,
        partition_by=request.partition_by,
        order_by=request.order_by,
        policy=policy,
        ch_only_shard=True,
        ch_replace_table=False,
        if_not_exists=False,
    )[0]
    # Every load/count/cutover explicitly addresses the configured storage scope.
    # A coordinator shadow is unnecessary and may be an unrelated local table.
    resource = ReplacementCreate(
        apply_query_label(sql, request.query_label), replacement_physical, policy.shard_on_cluster
    )
    submit_replacement_create(adapter, request, resource, on_resource_created)
    _wait_for_physical(request, resource)


def _wait_for_physical(request: Any, resource: ReplacementCreate) -> None:
    policy = request.ch_creation_policy
    if not waits_for_shard(policy.ddl_wait_policy):
        return
    if resource.cluster is None:
        _wait_for_ch_table(
            request.connection, resource.table, timeout_seconds=policy.ddl_ready_timeout_seconds
        )
        return
    _wait_for_ch_table_on_cluster(
        request.connection,
        resource.table,
        ch_cluster=resource.cluster,
        timeout_seconds=policy.ddl_ready_timeout_seconds,
    )


__all__ = ["ReplacementCreate", "create_replacement_physical", "submit_replacement_create"]
