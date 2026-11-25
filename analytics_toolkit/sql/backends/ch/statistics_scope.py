"""Bind internal statistics to verified target objects and live replica groups."""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError

from .dialect import sql_string_literal
from .outage_context import STATISTICS_TARGETS
from .outage_topology import query_rows
from .replication_identity import certify_replication_identity, require_replica_layout

if TYPE_CHECKING:
    from .outage_session import OutageSession

_MAX_PROOFS = 128
_REPLICA_FIELDS = 6


def preflight_statistics_targets(session: OutageSession) -> None:
    """Certify selected objects before an internal query can deduplicate replicas."""
    for cluster, database, table in STATISTICS_TARGETS.get():
        scope = session.scope(cluster)
        objects = scope.objects(database, table)
        selected = [item.host for item in objects if item.host in scope.reachable]
        replicated = bool(objects) and objects[0].engine.lower().startswith(
            ("replicated", "shared")
        )
        if objects and objects[0].engine.lower().startswith("replicated"):
            rows = query_rows(
                scope.client,
                "SELECT _shard_num, zookeeper_path, replica_name, is_readonly, "  # noqa: S608 - identities rendered as escaped literals.
                "is_session_expired, zookeeper_name "
                f"FROM clusterAllReplicas({sql_string_literal(scope.cluster)}, system, replicas) "
                f"WHERE database = {sql_string_literal(database)} "
                f"AND table = {sql_string_literal(table)}",
                settings=scope.probe_settings,
                text_columns=(1, 2, 5),
            )
            replicas = {int(row[0]): row for row in rows if len(row) >= _REPLICA_FIELDS}
            if len(replicas) != len(rows):
                message = "Incomplete or duplicate replica metadata for logical statistics."
                raise ClickHouseClusterTopologyError(message)
            require_replica_layout(selected, replicas, f"{database}.{table}")
            selected = [host for host in selected if not bool(replicas[host.position][4])]
            _certify_cached(session, scope, objects, selected, replicas, database, table)
        scope.record(
            selected,
            f"{database}.{table}",
            coverage_intent="logical_shards" if replicated else "physical_hosts",
        )


def _certify_cached(  # noqa: PLR0913 - cache binds the complete verified object and endpoint scope.
    session: OutageSession,
    scope: Any,
    objects: list[Any],
    selected: list[Any],
    replicas: dict[int, Any],
    database: str,
    table: str,
) -> None:
    # The bounded cache belongs to the operation, exact coordinator/configuration,
    # object definitions, native endpoints, and observed replica metadata.
    key = (
        id(session.client),
        id(session.config),
        scope.cluster,
        database,
        table,
        tuple(
            sorted(
                (item.host.position, item.uuid, item.engine, item.create_sql) for item in objects
            )
        ),
        tuple(
            (host.position, host.name, host.port, scope.observed_instances[host.position])
            for host in selected
        ),
        tuple((position, tuple(row)) for position, row in sorted(replicas.items())),
    )
    cache_key = ("ch_statistics_replication_proofs", session.config.connection_key)
    with session.state.lock:
        cache = session.state.resources.setdefault(cache_key, OrderedDict())
        if key in cache:
            cache.move_to_end(key)
            return
    certify_replication_identity(scope, selected, replicas)
    with session.state.lock:
        cache[key] = True
        cache.move_to_end(key)
        while len(cache) > _MAX_PROOFS:
            cache.popitem(last=False)
