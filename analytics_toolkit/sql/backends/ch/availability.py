"""Distinguish required physical storage from advisory deployment availability."""

from __future__ import annotations

import warnings
from typing import Any

from analytics_toolkit.sql.connection.errors import ClickHouseDegradedClusterWarning
from analytics_toolkit.sql.execution.backend_options import current_backend_operation

from .managed_routing import _resolve_cluster_name


def bind_creation_availability(connection: Any, policy: Any) -> None:
    """Keep explicit local overrides and the resolved policy authoritative."""
    if policy is None or getattr(connection, "_ch_outage_client", False) is not True:
        return
    state = current_backend_operation()
    if state is not None:
        state.resources[("ch_availability_policy", connection._config.connection_key)] = policy  # noqa: SLF001 -- one backend's composed client.


def bind_session_availability(session: Any, policy: Any) -> None:
    session.state.resources[("ch_availability_policy", session.config.connection_key)] = policy


def _required_cluster(config: Any, state: Any) -> tuple[str | None, bool]:
    policy = state.resources.get(("ch_availability_policy", config.connection_key))
    if policy is not None:
        return policy.shard_on_cluster, policy.ddl_wait_policy in {"wait_all", "wait_distr"}
    defaults = getattr(getattr(config, "ddl_defaults", None), "regular", None)
    cluster = getattr(getattr(defaults, "shard", None), "on_cluster", None)
    explicit_wait = getattr(config, "ch_ddl_wait_policy", None)
    return (
        cluster if isinstance(cluster, str) and cluster else None,
        explicit_wait in {"wait_all", "wait_distr"},
    )


def optional_cluster(scope: Any) -> bool:
    """A missing physical placement never makes arbitrary scopes optional."""
    cluster, require_facades = _required_cluster(scope.config, scope.state)
    if cluster is None or require_facades or scope.policy != "stop":
        return False
    required = _resolve_cluster_name(scope.client, cluster)
    if scope.cluster == required:
        return False
    # Different cluster names may describe exactly the same physical instances.
    # Treat those aliases as required storage too, without trusting name equality.
    from .dialect import sql_string_literal  # noqa: PLC0415 -- metadata import cycle.
    from .outage_topology import query_rows  # noqa: PLC0415 -- scope construction cycle.

    rows = query_rows(
        scope.client,
        "SELECT shard_num, replica_num, host_name, port, is_local FROM system.clusters "  # noqa: S608 -- escaped cluster literal.
        f"WHERE cluster = {sql_string_literal(required)} ORDER BY shard_num, replica_num",
        text_columns=(2,),
    )
    required_endpoints = {(str(row[2]), int(row[3])) for row in rows}
    actual_endpoints = {(host.name, host.port) for host in scope.hosts}
    if not required_endpoints:
        from analytics_toolkit.sql.connection.errors import (  # noqa: PLC0415 -- backend cycle.
            ClickHouseClusterTopologyError,
        )

        message = f"Required shard cluster {required!r} has no configured hosts."
        raise ClickHouseClusterTopologyError(message)
    return actual_endpoints != required_endpoints


def warn_optional_availability(scope: Any, reason: str) -> None:
    missing = [host.identity for host in scope.hosts if host not in scope.reachable]
    if not missing:
        return
    key = ("optional_cluster_availability", scope.config.connection_key, scope.cluster)
    with scope.state.lock:
        if key in scope.state.warned:
            return
        scope.state.warned.add(key)
    message = (
        f"ClickHouse cluster {scope.cluster!r} is not required for physical shard storage; "
        f"continuing {reason} using {len(scope.reachable)}/{len(scope.hosts)} hosts. "
        f"Unavailable: {', '.join(missing)}. Deployment on those hosts is not confirmed."
    )
    warnings.warn(message, ClickHouseDegradedClusterWarning, stacklevel=3)


def record_optional_availability(scope: Any, table: str | None = None) -> None:
    """Report host coverage without claiming that optional facades contain data."""
    status = scope.record(scope.reachable, table, coverage_intent="optional_execution_hosts")
    with scope.state.lock:
        status["availability_required"] = False
        status["complete"] = True


__all__ = [
    "bind_creation_availability",
    "bind_session_availability",
    "optional_cluster",
    "record_optional_availability",
    "warn_optional_availability",
]
