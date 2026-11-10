"""Drop verified policy resources with independent placement and bounded recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlglot import exp, parse_one

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.connection.get_sql_connection import get_ch_connection_for_host
from analytics_toolkit.sql.execution.backend_options import current_backend_query_label

from .dialect import ClickHouseDialect
from .lifecycle import ChDistributedTablePair, _drop_ch_distributed_table_pair_on_cluster_hosts
from .wait import _wait_for_ch_table_absence, _wait_for_ch_table_absence_on_cluster

if TYPE_CHECKING:
    from collections.abc import Sequence

_HOST_DROP_WORKERS = 5


@dataclass(frozen=True)
class DropResource:
    table: str
    cluster: str | None


def execute_policy_target_drop(
    adapter: Any,
    connection: Any,
    drop_sqls: Sequence[str],
    *,
    target_key: str,
    retry_per_host_drops: bool = True,
) -> None:
    if not drop_sqls:
        return
    resources = list(dict.fromkeys(_drop_resource(sql) for sql in drop_sqls))
    adapter.execute_commands(connection, list(drop_sqls))
    for resource in resources:
        _wait_with_recovery(
            connection, resource, target_key=target_key, retry_per_host_drops=retry_per_host_drops
        )


def _wait_with_recovery(
    connection: Any,
    resource: DropResource,
    *,
    target_key: str,
    retry_per_host_drops: bool,
) -> None:
    try:
        _wait_absent(connection, resource)
    except TimeoutError:
        if resource.cluster is None or not retry_per_host_drops:
            raise
        # Explicit identical names reuse endpoint-aware recovery without deriving
        # a companion. The per-host executor deduplicates identical commands.
        _drop_ch_distributed_table_pair_on_cluster_hosts(
            connection,
            ChDistributedTablePair(resource.table, resource.table),
            ch_cluster=resource.cluster,
            query_label=current_backend_query_label(),
            per_host_drop_workers=_HOST_DROP_WORKERS,
            per_host_connection_factory=lambda host: get_ch_connection_for_host(target_key, host),
        )
        _wait_absent(connection, resource)


def _wait_absent(connection: Any, resource: DropResource) -> None:
    if resource.cluster is None:
        _wait_for_ch_table_absence(connection, resource.table)
    else:
        _wait_for_ch_table_absence_on_cluster(
            connection, resource.table, ch_cluster=resource.cluster
        )


def _drop_resource(sql: str) -> DropResource:
    tree = parse_one(sql, read=ClickHouseDialect)
    tables = tree.args.get("tables") or [tree.this]
    if (
        not isinstance(tree, exp.Drop)
        or str(tree.args.get("kind", "")).upper() != "TABLE"
        or len(tables) != 1
        or not isinstance(tables[0], exp.Table)
        or not isinstance(tables[0].this, exp.Identifier)
    ):
        message = "Policy cleanup requires one static DROP TABLE resource per statement."
        raise InvalidSqlInputError(message)
    cluster = tree.args.get("cluster")
    if cluster is not None and (
        not isinstance(cluster, exp.OnCluster)
        or not isinstance(cluster.this, (exp.Literal, exp.Identifier))
    ):
        message = "Policy cleanup requires a static cluster identifier."
        raise InvalidSqlInputError(message)
    return DropResource(
        tables[0].sql(dialect="clickhouse"),
        cluster.this.name if cluster is not None else None,
    )


__all__ = ["execute_policy_target_drop"]
