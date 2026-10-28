"""Prove selected-host Keeper identities before any replicated table is created."""

from __future__ import annotations

from string import Formatter
from typing import TYPE_CHECKING, Any

from sqlglot import exp

from analytics_toolkit.sql.backends.utils import sql_string_literal
from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError

from .outage_hosts import host_connection
from .outage_topology import query_rows

if TYPE_CHECKING:
    from .outage_topology import ClusterScope, TableObject

_IDENTITY_ARGUMENTS = 2


def _expand(template: str, macros: dict[str, str]) -> str:
    try:
        for literal, field, specification, conversion in Formatter().parse(template):
            if "{" in literal or "}" in literal:
                msg = "Escaped replica macros cannot be verified safely."
                raise ClickHouseClusterTopologyError(msg)
            if field is not None and (
                specification
                or conversion
                or field not in macros
                or "{" in macros[field]
                or "}" in macros[field]
            ):
                msg = "Replica identity contains an unverified macro."
                raise ClickHouseClusterTopologyError(msg)
        return template.format_map(macros)
    except (KeyError, ValueError) as exc:
        msg = "Cannot expand the replica identity safely."
        raise ClickHouseClusterTopologyError(msg) from exc


def _templates(connection: Any, engine: Any) -> tuple[str, str]:
    arguments = list(engine.expressions)
    strings = [
        item
        for item in arguments[:_IDENTITY_ARGUMENTS]
        if isinstance(item, exp.Literal) and item.is_string
    ]
    if len(strings) == _IDENTITY_ARGUMENTS:
        return str(strings[0].this), str(strings[1].this)
    if strings:
        msg = "Replicated engine requires both a Keeper path and a replica name."
        raise ClickHouseClusterTopologyError(msg)
    try:
        defaults = dict(
            query_rows(
                connection,
                "SELECT name, value FROM system.server_settings "
                "WHERE name IN ('default_replica_path', 'default_replica_name')",
            )
        )
        return str(defaults["default_replica_path"]), str(defaults["default_replica_name"])
    except Exception as exc:
        msg = (
            "Cannot verify server replica defaults; specify an explicit macro-based "
            "ReplicatedMergeTree Keeper path and replica name."
        )
        raise ClickHouseClusterTopologyError(msg) from exc


def validate_replica_creation(
    scope: ClusterScope,
    statement: exp.Create,
    database: str,
    table: str,
    existing: list[TableObject],
) -> None:
    property_ = statement.find(exp.EngineProperty)
    engine = property_.this if property_ is not None else None
    if engine is None or not engine.name.lower().startswith("replicated"):
        return
    uuid = statement.find(exp.UuidProperty)
    table_uuid = str(uuid.this.this) if uuid is not None else ""
    identities: dict[int, tuple[str, str]] = {}
    shard_paths: dict[int, str] = {}
    path_shards: dict[str, int] = {}
    replicas: set[tuple[str, str]] = set()
    for host in scope.reachable:
        with host_connection(scope, host) as connection:
            path_template, replica_template = _templates(connection, engine)
            if len({item.shard for item in scope.hosts}) > 1 and "{shard}" not in path_template:
                msg = "Healthy-only replicated creation requires a shard-separated Keeper path."
                raise ClickHouseClusterTopologyError(msg)
            if not existing and "{uuid}" not in path_template:
                msg = "A new healthy-only replicated table requires {uuid} in its Keeper path."
                raise ClickHouseClusterTopologyError(msg)
            macros = {
                str(name): str(value)
                for name, value in query_rows(
                    connection, "SELECT macro, substitution FROM system.macros"
                )
            }
        # ClickHouse lets explicit server macros override database/table built-ins.
        macros.setdefault("database", database)
        macros.setdefault("table", table)
        if "uuid" in macros and macros["uuid"] != table_uuid:
            msg = "A server macro shadows the table UUID; replica-group isolation is unverified."
            raise ClickHouseClusterTopologyError(msg)
        if table_uuid:
            macros["uuid"] = table_uuid
        path, replica = _expand(path_template, macros), _expand(replica_template, macros)
        if (
            not path
            or not replica
            or (path, replica) in replicas
            or (host.shard in shard_paths and shard_paths[host.shard] != path)
            or (path in path_shards and path_shards[path] != host.shard)
        ):
            msg = "Replicated creation would reuse an unsafe Keeper path or replica identity."
            raise ClickHouseClusterTopologyError(msg)
        identities[host.position] = (path, replica)
        shard_paths[host.shard], path_shards[path] = path, host.shard
        replicas.add((path, replica))
    if existing:
        rows = query_rows(
            scope.client,
            "SELECT _shard_num, zookeeper_path, replica_name "  # noqa: S608 - escaped literals
            f"FROM clusterAllReplicas({sql_string_literal(scope.cluster)}, system, replicas) "
            f"WHERE database = {sql_string_literal(database)} "
            f"AND table = {sql_string_literal(table)}",
            settings=scope.probe_settings,
        )
        observed = {int(row[0]): (str(row[1]), str(row[2])) for row in rows}
        if any(
            observed.get(item.host.position) != identities[item.host.position] for item in existing
        ):
            msg = (
                "Existing replica identities disagree with the "
                "destination-specific creation schema."
            )
            raise ClickHouseClusterTopologyError(msg)
