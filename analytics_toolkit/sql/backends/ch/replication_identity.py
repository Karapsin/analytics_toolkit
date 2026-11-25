"""Certify common live replication state through cross-peer Keeper visibility."""

from __future__ import annotations

from numbers import Integral
from typing import TYPE_CHECKING, Any
from uuid import UUID

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError
from analytics_toolkit.sql.dml.transfer.runtime.retry import is_non_retryable_sql_error
from analytics_toolkit.sql.execution.backend_options import current_backend_query_label
from analytics_toolkit.sql.execution.labels import apply_query_label

from .dialect import sql_string_literal
from .metadata_values import metadata_text
from .outage_hosts import host_connection

if TYPE_CHECKING:
    from .outage_topology import ClusterScope, Host

_LEASE_FIELDS = 2
_MIN_PEERS = 2
_REPLICA_FIELDS = 6


def require_replica_layout(selected: list[Host], replica_rows: dict[int, Any], table: str) -> None:
    """Validate structural replica identities before comparing live leases."""
    paths: dict[str, int] = {}
    shard_paths: dict[int, str] = {}
    names: set[tuple[str, str]] = set()
    for host in selected:
        row = replica_rows.get(host.position)
        if row is None:
            message = f"Missing replica metadata for {table}."
            raise ClickHouseClusterTopologyError(message)
        path, replica = str(row[1]), str(row[2])
        if (
            (path in paths and paths[path] != host.shard)
            or (host.shard in shard_paths and shard_paths[host.shard] != path)
            or (path, replica) in names
        ):
            message = f"Unsafe replica identities for {table}."
            raise ClickHouseClusterTopologyError(message)
        paths[path] = host.shard
        shard_paths[host.shard] = path
        names.add((path, replica))


def _lease(rows: list[Any], expected_server: str) -> tuple[str, int]:
    if len(rows) != 1 or len(rows[0]) != _LEASE_FIELDS:
        message = "Incomplete Keeper replication lease metadata."
        raise ClickHouseClusterTopologyError(message)
    value, owner = rows[0]
    value = metadata_text(value)
    if not value.startswith("UUID_'") or not value.endswith("'"):
        message = "Cannot verify the Keeper replica's active server identity."
        raise ClickHouseClusterTopologyError(message)
    if isinstance(owner, bool) or not isinstance(owner, (Integral, str)):
        message = "Malformed Keeper replication lease session identity."
        raise ClickHouseClusterTopologyError(message)
    try:
        server = str(UUID(value[6:-1]))
        session = int(owner)
    except (TypeError, ValueError) as exc:
        message = "Malformed Keeper replication lease identity."
        raise ClickHouseClusterTopologyError(message) from exc
    if server != expected_server or session == 0:
        message = "Keeper replication lease does not belong to the observed server instance."
        raise ClickHouseClusterTopologyError(message)
    return server, session


def _raise_probe_error(exc: Exception) -> None:
    permission_failure = isinstance(exc, PermissionError) or "denied" in str(exc).lower()
    if not permission_failure and not is_non_retryable_sql_error(exc):
        # A transport failure remains eligible for the operation's ordinary
        # retry with a fresh coordinator and complete proof.
        raise exc
    message = (
        "Cannot certify shared Keeper replication identity. Ensure each selected "
        "host can read its actual replica namespace through system.zookeeper."
    )
    raise ClickHouseClusterTopologyError(message) from exc


def certify_replication_identity(
    scope: ClusterScope, selected: list[Host], replica_rows: dict[int, Any]
) -> None:
    """Require every selected peer to observe each actual replica's same live lease.

    ReplicatedMergeTree writes its server UUID into the ephemeral `is_active`
    node. A shared path string or auxiliary Keeper alias alone proves no common
    coordination domain. Single selected replicas need no deduplication proof.
    """
    groups: dict[int, list[Host]] = {}
    for host in selected:
        groups.setdefault(host.shard, []).append(host)
    for peers in groups.values():
        if len(peers) < _MIN_PEERS:
            continue
        reference: dict[int, tuple[str, int]] | None = None
        for observer in peers:
            row = replica_rows[observer.position]
            if len(row) < _REPLICA_FIELDS:
                message = "Cannot certify the replica's actual Keeper namespace."
                raise ClickHouseClusterTopologyError(message)
            keeper = metadata_text(row[5])
            observed: dict[int, tuple[str, int]] = {}
            try:
                with host_connection(scope, observer) as connection:
                    for member in peers:
                        replica = replica_rows[member.position]
                        path = f"{str(replica[1]).rstrip('/')}/replicas/{replica[2]}"
                        query = (
                            "SELECT value, ephemeralOwner FROM system.zookeeper "  # noqa: S608 - metadata paths rendered as escaped literals.
                            f"WHERE path = {sql_string_literal(path)} AND name = 'is_active' "
                            f"AND zookeeperName = {sql_string_literal(keeper)}"
                        )
                        result = connection.query(
                            apply_query_label(query, current_backend_query_label()),
                            settings=scope.probe_settings,
                        )
                        observed[member.position] = _lease(
                            list(result.result_rows), scope.observed_instances[member.position][0]
                        )
            except ClickHouseClusterTopologyError:
                raise
            except Exception as exc:  # noqa: BLE001 - classify backend errors while retaining transient replay.
                _raise_probe_error(exc)
            if reference is not None and observed != reference:
                message = "Selected replicas do not share the same live Keeper replication leases."
                raise ClickHouseClusterTopologyError(message)
            reference = observed
