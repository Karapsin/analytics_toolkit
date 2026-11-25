"""Endpoint and ownership preflight for structural table migrations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from .reconfigure_facade import FacadeMutation, validate_facade_destination
from .reconfigure_facade_scope import inspect_facade_scope
from .reconfigure_routes import require_desired_facade_routing
from .reconfigure_support import cluster_hosts, resolve_optional_cluster, table_exists_on_cluster
from .scope_identity import grouped_scope_identity


def endpoints(connection: Any, cluster: str) -> set[tuple[str, int]]:
    name = resolve_optional_cluster(connection, cluster)
    if name is None:
        message = "Cannot resolve replacement cluster endpoints."
        raise InvalidSqlInputError(message)
    hosts = cluster_hosts(connection, name)
    if not hosts:
        message = f"Cannot verify ClickHouse cluster {cluster!r} endpoints."
        raise InvalidSqlInputError(message)
    return {(address or host, port) for host, address, port in hosts}


@dataclass(frozen=True)
class DestinationTopology:
    source_pair: bool
    target_pair: bool
    source_cluster: str | None
    target_cluster: str | None
    distributed_cluster: str | None
    desired_shard_table: str
    cross_cluster: bool
    copy_required: bool = False
    logical_table: str | None = None
    facade_cluster: str | None = None
    facade_mutation: bool = False
    source_facade_ddl: str | None = None
    connection_key: str | None = None


def validate_destination(connection: Any, topology: DestinationTopology) -> None:
    source_pair = topology.source_pair
    target_pair = topology.target_pair
    source_cluster = topology.source_cluster
    target_cluster = topology.target_cluster
    distributed_cluster = topology.distributed_cluster
    desired_shard_table = topology.desired_shard_table
    cross_cluster = topology.cross_cluster
    if source_pair != target_pair or (source_pair and (topology.facade_mutation or cross_cluster)):
        validate_facade_destination(
            connection,
            FacadeMutation(
                table=topology.logical_table,
                scope=topology.facade_cluster,
                source_ddl=topology.source_facade_ddl if source_pair else None,
                source_cluster=source_cluster,
                physical_scope=target_cluster,
                pair_to_local=source_pair and not target_pair,
            ),
        )
    if target_pair and not source_pair:
        if target_cluster is None or distributed_cluster is None:
            message = "Local-to-pair conversion requires verified physical and routing clusters."
            raise InvalidSqlInputError(message)
        if table_exists_on_cluster(connection, desired_shard_table, target_cluster):
            message = (
                f"Destination already contains {desired_shard_table}; refusing to overwrite it."
            )
            raise InvalidSqlInputError(message)
    if not target_pair or (not cross_cluster and source_pair and not topology.copy_required):
        return
    if target_cluster is None or distributed_cluster is None:
        message = "Cannot verify replacement storage routing."
        raise InvalidSqlInputError(message)
    physical = endpoints(connection, target_cluster)
    routed = endpoints(connection, distributed_cluster)
    if routed != physical:
        message = (
            "Distributed copy routing must reach exactly the verified "
            "replacement storage endpoints."
        )
        raise InvalidSqlInputError(message)
    physical_scope = inspect_facade_scope(connection, target_cluster, coordinator_required=False)
    routing_scope = inspect_facade_scope(
        connection, distributed_cluster, coordinator_required=False
    )
    if grouped_scope_identity(physical_scope) != grouped_scope_identity(routing_scope):
        message = "Distributed copy routing must preserve the verified storage shard groups."
        raise InvalidSqlInputError(message)
    if (
        cross_cluster
        and source_cluster is not None
        and routed & endpoints(connection, source_cluster)
    ):
        message = "Replacement copy routing reaches original source storage."
        raise InvalidSqlInputError(message)
    require_desired_facade_routing(connection, topology, physical_scope)


__all__ = ["validate_destination"]
