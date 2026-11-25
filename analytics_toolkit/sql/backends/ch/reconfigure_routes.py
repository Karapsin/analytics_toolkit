"""Prove the desired routing from every actual facade server before rebuilding."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from .reconfigure_facade_scope import inspect_facade_scope
from .reconfigure_support import resolve_optional_cluster
from .scope_hosts import verified_scope_host
from .scope_identity import grouped_scope_identity

if TYPE_CHECKING:
    from analytics_toolkit.sql.connection.config import ChConfig

    from .reconfigure_facade_scope import FacadeScope
    from .reconfigure_topology import DestinationTopology


def require_desired_facade_routing(
    connection: Any, topology: DestinationTopology, physical: FacadeScope
) -> None:
    if topology.facade_cluster is None:
        return
    if topology.connection_key is None:
        message = "Cannot verify configuration identity for desired facade routing."
        raise InvalidSqlInputError(message)
    config = cast("ChConfig", get_connection_config(topology.connection_key))
    facades = inspect_facade_scope(connection, topology.facade_cluster, coordinator_required=False)
    expected = grouped_scope_identity(physical)
    for position in facades.identities:
        with verified_scope_host(connection, facades, config, position) as worker:
            cluster = resolve_optional_cluster(worker, topology.distributed_cluster)
            if cluster is None:
                message = "Cannot verify desired Distributed routing on every facade host."
                raise InvalidSqlInputError(message)
            actual = inspect_facade_scope(worker, cluster, coordinator_required=False)
            if grouped_scope_identity(actual) != expected:
                message = "Desired facade routing differs from the verified storage shard groups."
                raise InvalidSqlInputError(message)


__all__ = ["require_desired_facade_routing"]
