"""Complete physical ownership and copy intent for structural reconfiguration."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, overload

from sqlglot import exp

from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.execution.labels import apply_query_label

from .dialect import ClickHouseDialect
from .reconfigure_ddl import (
    comparable_create_sql,
    engine_name,
    parse_create_table,
    require_merge_tree,
    retarget_create,
)
from .reconfigure_facade_scope import (
    FacadeScope,
    inspect_facade_scope,
    occupied_definitions,
    read_rows,
)

if TYPE_CHECKING:
    from .reconfigure_facade_scope import ServerIdentity
    from .reconfigure_models import ChReconfiguration, ChReconfigureOptions

_MEMBER_FIELDS = 3
_NON_PHYSICAL_STRATEGIES = {"no_op", "wrapper_recreate"}


@dataclass(frozen=True)
class PhysicalStorage:
    table: str
    ddl: str
    engine: str

    @property
    def replicated(self) -> bool:
        return self.engine.casefold().startswith(("replicated", "shared"))


@dataclass(frozen=True)
class ClusterPhysicalStorage(PhysicalStorage):
    scope: FacadeScope
    shards: tuple[int, ...]
    hostnames: tuple[str, ...]

    @property
    def independent_replicas(self) -> bool:
        return not self.replicated and any(count > 1 for count in Counter(self.shards).values())


@dataclass(frozen=True)
class PhysicalCopyEndpoint:
    position: int
    hostname: str
    native: tuple[str, int]
    identity: ServerIdentity
    coordinator: bool


@dataclass(frozen=True)
class PhysicalCopyPlan:
    source: str
    replacement: str
    sql: str
    endpoints: tuple[PhysicalCopyEndpoint, ...]


@dataclass(frozen=True)
class PhysicalRebuildPayload:
    replacement: str
    copy: PhysicalCopyPlan | None = None


@dataclass
class PhysicalExecutionScope:
    source: PhysicalStorage
    target: PhysicalStorage
    config: Any = field(repr=False)
    connections: dict[int, Any] = field(default_factory=dict, repr=False, init=False)
    counts: dict[str, dict[str, int]] = field(default_factory=dict, init=False)


@dataclass
class PhysicalRebuildExecutionScope(PhysicalExecutionScope):
    payload: PhysicalRebuildPayload


def _table_name(ddl: str) -> str:
    create = parse_create_table(ddl, "physical reconfiguration storage")
    schema = create.this
    if not isinstance(schema, exp.Schema) or not isinstance(schema.this, exp.Table):
        message = "Cannot verify ClickHouse physical table identity."
        raise InvalidSqlInputError(message)
    return schema.this.sql(dialect=ClickHouseDialect)


def _members(connection: Any, scope: FacadeScope) -> tuple[tuple[int, ...], tuple[str, ...]]:
    statement = (
        exp.select("shard_num", "replica_num", "host_name")
        .from_("system.clusters")
        .where(exp.column("cluster").eq(exp.Literal.string(scope.cluster)))
        .order_by("shard_num", "replica_num")
    )
    rows = read_rows(connection, statement.sql(dialect=ClickHouseDialect))
    message = "Cannot verify complete physical source endpoint membership."
    if len(rows) != len(scope.endpoints) or any(len(row) != _MEMBER_FIELDS for row in rows):
        raise InvalidSqlInputError(message)
    try:
        members = tuple((int(row[0]), int(row[1])) for row in rows)
        names = tuple(str(row[2]) for row in rows)
    except (TypeError, ValueError) as exc:
        raise InvalidSqlInputError(message) from exc
    if (
        len(set(members)) != len(members)
        or any(shard <= 0 or replica <= 0 for shard, replica in members)
        or any(not name for name in names)
    ):
        raise InvalidSqlInputError(message)
    observed = tuple((shard, replica, name) for (shard, replica), name in zip(members, names))
    if observed != scope.membership:
        message = "Physical source endpoint membership changed during scope certification."
        raise InvalidSqlInputError(message)
    return tuple(shard for shard, _replica in members), names


def certify_physical_table(connection: Any, storage: PhysicalStorage, table: str) -> None:
    """Require exact endpoint presence, not merely reachable hosts and matching survivors."""
    if not isinstance(storage, ClusterPhysicalStorage):
        return
    definitions = occupied_definitions(connection, table, storage.scope)
    if set(definitions) != set(storage.scope.identities):
        message = "Incomplete physical source table presence on the full execution scope."
        raise InvalidSqlInputError(message)
    expected = retarget_create(parse_create_table(storage.ddl, storage.table), table, None)
    canonical = comparable_create_sql(expected)
    if any(
        comparable_create_sql(parse_create_table(ddl, table)) != canonical
        for ddl in definitions.values()
    ):
        message = "Physical source DDL differs across the verified execution scope."
        raise InvalidSqlInputError(message)


@overload
def _storage(connection: Any, ddl: str, cluster: str) -> ClusterPhysicalStorage: ...


@overload
def _storage(connection: Any, ddl: str, cluster: None) -> PhysicalStorage: ...


def _storage(connection: Any, ddl: str, cluster: str | None) -> PhysicalStorage:
    table = _table_name(ddl)
    create = parse_create_table(ddl, table)
    if cluster is None:
        return PhysicalStorage(table, ddl, engine_name(create))
    scope = inspect_facade_scope(connection, cluster, coordinator_required=False)
    shards, hostnames = _members(connection, scope)
    return ClusterPhysicalStorage(table, ddl, engine_name(create), scope, shards, hostnames)


def _replacement(change: ChReconfiguration) -> str:
    # Every staged physical strategy creates its storage first; settings has no
    # staged payload and never calls this constructor.
    statement = next(
        statement for statement in change.plan.statements if statement.phase == "create_replacement"
    )
    table = _table_name(statement.sql)
    require_merge_tree(parse_create_table(statement.sql, table), table)
    return table


def _same_placement(source: ClusterPhysicalStorage, target: ClusterPhysicalStorage) -> bool:
    source_identities = {
        endpoint.native: source.scope.identities[position]
        for position, endpoint in enumerate(source.scope.endpoints, 1)
    }
    target_identities = {
        endpoint.native: target.scope.identities[position]
        for position, endpoint in enumerate(target.scope.endpoints, 1)
    }
    return source_identities == target_identities


def _require_distinguishable_http_hosts(config: Any, source: ClusterPhysicalStorage) -> None:
    if config.driver == "native":
        return
    ports: dict[str, set[int]] = {}
    for hostname, endpoint in zip(source.hostnames, source.scope.endpoints):
        ports.setdefault(hostname, set()).add(endpoint.native[1])
    if any(len(values) > 1 for values in ports.values()):
        message = (
            "The HTTP endpoint cannot distinguish the configured physical native instances; "
            "use independently addressable endpoints for an explicit per-host migration."
        )
        raise InvalidSqlInputError(message)


def _endpoint_copy(
    options: ChReconfigureOptions,
    change: ChReconfiguration,
    execution: PhysicalExecutionScope,
    replacement: str,
) -> PhysicalCopyPlan | None:
    source, target = execution.source, execution.target
    if not isinstance(source, ClusterPhysicalStorage) or not source.independent_replicas:
        return None
    if (
        change.strategy != "managed_pair_rebuild"
        or not isinstance(target, ClusterPhysicalStorage)
        or not _same_placement(source, target)
        or target.replicated
    ):
        message = (
            "Independent physical replica datasets require a verified unchanged endpoint "
            "bijection and nonreplicated destination storage. Use an explicit per-host "
            "migration for relocation, topology conversion, or replication changes."
        )
        raise InvalidSqlInputError(message)
    _require_distinguishable_http_hosts(execution.config, source)
    sql = apply_query_label(
        f"INSERT INTO {replacement} SELECT * FROM {source.table}",  # noqa: S608 - parser-rendered table identities
        options.query_label,
    )
    endpoints = tuple(
        PhysicalCopyEndpoint(
            position,
            source.hostnames[position - 1],
            endpoint.native,
            source.scope.identities[position],
            position == source.scope.coordinator,
        )
        for position, endpoint in enumerate(source.scope.endpoints, 1)
    )
    return PhysicalCopyPlan(source.table, replacement, sql, endpoints)


def _record_endpoint_sql(change: ChReconfiguration, copy: PhysicalCopyPlan) -> None:
    original = next(
        statement for statement in change.plan.statements if statement.phase == "copy_data"
    )
    change.plan.statements = [
        statement for statement in change.plan.statements if statement.phase != "copy_data"
    ]
    insert_at = next(
        index
        for index, statement in enumerate(change.plan.statements)
        if statement.phase == "cutover"
    )
    change.plan.statements[insert_at:insert_at] = [
        replace(original, sql=copy.sql, phase="copy_data_endpoint") for _ in copy.endpoints
    ]
    change.plan.options["endpoint_scoped_sql"] = [
        {"host": endpoint.hostname, "native_port": endpoint.native[1], "sql": copy.sql}
        for endpoint in copy.endpoints
    ]


def prepare_physical_execution(
    connection: Any, options: ChReconfigureOptions, change: ChReconfiguration
) -> None:
    """Certify metadata only, after the actual mutating strategy is known."""
    if change.strategy in _NON_PHYSICAL_STRATEGIES:
        return
    source = _storage(
        connection,
        change.before_ddl["shard" if change.source_pair else "table"],
        change.source_cluster_resolved if change.source_pair else None,
    )
    certify_physical_table(connection, source, source.table)
    target = _storage(
        connection,
        change.after_ddl["shard" if change.target_pair else "table"],
        change.target_cluster_resolved,
    )
    config = get_connection_config(options.connection_key)
    execution = PhysicalExecutionScope(source, target, config)
    copy: PhysicalCopyPlan | None = None
    if change.strategy != "settings":
        replacement = _replacement(change)
        copy = _endpoint_copy(options, change, execution, replacement)
        execution = PhysicalRebuildExecutionScope(
            source, target, config, PhysicalRebuildPayload(replacement, copy)
        )
        if copy is not None:
            _record_endpoint_sql(change, copy)
    change.plan.options["physical_coverage_intent"] = (
        "logical_shards" if source.replicated else "physical_hosts"
    )
    change.plan.options["physical_copy_mode"] = (
        "endpoint_local" if copy is not None else "native_sql"
    )
    change.physical_execution = execution


def require_physical_identity(connection: Any, execution: PhysicalExecutionScope) -> None:
    """Recheck the selected configuration and full metadata scope before DDL."""
    if get_connection_config(execution.config.connection_key) != execution.config:
        message = "ClickHouse configuration changed after physical reconfiguration planning."
        raise InvalidSqlInputError(message)
    for storage in (execution.source, execution.target):
        if not isinstance(storage, ClusterPhysicalStorage):
            continue
        observed = _storage(connection, storage.ddl, storage.scope.cluster)
        if (
            observed.scope != storage.scope
            or observed.shards != storage.shards
            or observed.hostnames != storage.hostnames
        ):
            message = "Physical source endpoint scope changed after reconfiguration planning."
            raise InvalidSqlInputError(message)
    certify_physical_table(connection, execution.source, execution.source.table)


__all__ = [
    "ClusterPhysicalStorage",
    "PhysicalCopyEndpoint",
    "PhysicalCopyPlan",
    "PhysicalExecutionScope",
    "PhysicalRebuildExecutionScope",
    "PhysicalRebuildPayload",
    "certify_physical_table",
    "prepare_physical_execution",
    "require_physical_identity",
]
