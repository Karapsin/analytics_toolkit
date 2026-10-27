"""Resolve physical routes and eligible insertion shards for one operation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlglot import exp, parse_one

from analytics_toolkit.sql.backends.transfer_stage import is_transfer_stage_identifier
from analytics_toolkit.sql.backends.utils import sql_string_literal
from analytics_toolkit.sql.connection.ddl_defaults import _MISSING
from analytics_toolkit.sql.connection.errors import (
    ClickHouseClusterTopologyError,
    ClickHouseClusterUnavailableError,
)

from .managed_routing import ManagedTableRoute
from .metadata import extract_clickhouse_distributed_shard_table, extract_clickhouse_function_args
from .outage_context import INSERT_KEY, WRITING
from .outage_ddl import provision_missing
from .outage_topology import ClusterScope, Host, TableObject, query_rows, warn_degraded
from .routing import ChClusterRouting, _RoutingClickHouse

if TYPE_CHECKING:
    from analytics_toolkit.sql.execution.backend_options import BackendOperationState


_DISTRIBUTED_KEY_ARGUMENTS = 4


def configured_cluster(config: Any) -> str | None:
    routing = getattr(config, "cluster_routing", None)
    if routing is not None:
        return str(routing.cluster)
    defaults = getattr(config, "ddl_defaults", None)
    regular = getattr(defaults, "regular", None)
    distributed = getattr(regular, "distributed", None)
    value = getattr(distributed, "cluster", None)
    return value if isinstance(value, str) and value else None


def private_table(name: str) -> bool:
    return is_transfer_stage_identifier(name) or "__replace_" in name or "__stage__" in name


class OutageSession:
    def __init__(
        self, client: Any, config: Any, state: BackendOperationState, base_policy: str | None = None
    ) -> None:
        self.client, self.config, self.state = client, config, state
        self.policy: str = str(
            state.ch_unavailable_shards
            or base_policy
            or getattr(config, "ch_unavailable_shards", "stop")
        )
        self.scopes: dict[str, ClusterScope] = {}
        self.insert_scope: ClusterScope | None = None
        self.insert_hosts: list[Host] = []
        self.insert_all_replicas = False
        self.insert_key = "rand()"

    def scope(self, cluster: str) -> ClusterScope:
        if cluster not in self.scopes:
            self.scopes[cluster] = ClusterScope(
                self.client, self.config, cluster, self.state, self.policy
            )
        return self.scopes[cluster]

    def invalidate(self) -> None:
        # Metadata is read afresh; only membership is scoped to the current operation.
        return None

    def resolve(self, *, cluster: str, database: str, table: str) -> ManagedTableRoute:  # noqa: C901, PLR0912, PLR0915 - keep the safety decision and submission in one boundary
        scope = self.scope(cluster)
        writing = WRITING.get()
        if database.lower() == "system":
            if self.policy == "stop":
                scope.require_all(scope.reachable, "System metadata")
            elif len(scope.reachable) != len(scope.hosts):
                warn_degraded(
                    self.state, self.config, scope.record(scope.reachable), "read metadata"
                )
            return ManagedTableRoute(database, table, "all_replicas")
        objects = scope.objects(database, table)
        local_facade = False
        if not objects:
            objects = self.local_facade(database, table)
            local_facade = bool(objects)
        if not objects:
            msg = f"Table {database}.{table} has no usable schema."
            raise ClickHouseClusterUnavailableError(msg)
        logical_objects = objects
        sharding_key = getattr(
            getattr(self.config, "cluster_routing", None), "sharding_key", "rand()"
        )
        engine = objects[0].engine
        routed_table = table
        if engine.lower() == "distributed":
            shard = extract_clickhouse_distributed_shard_table(objects[0].engine_full, database)
            if shard is None or shard.database != database or shard.table != f"{table}_shard":
                if self.policy == "healthy_shards":
                    msg = "Ad hoc routing requires a standard managed table pair."
                    raise ClickHouseClusterTopologyError(msg)
                scope.require_all(scope.reachable, "Distributed facade")
                return ManagedTableRoute(database, table, "local", engine)
            if self.scope(shard.cluster).cluster != scope.cluster:
                if self.policy == "healthy_shards":
                    msg = "Ad hoc routing requires the facade and data routing clusters to agree."
                    raise ClickHouseClusterTopologyError(msg)
                scope.require_all(scope.reachable, "Distributed facade")
                scope = self.scope(shard.cluster)
                local_facade = True
            args = extract_clickhouse_function_args(objects[0].engine_full, "Distributed")
            sharding_key = (
                args[3] if args is not None and len(args) >= _DISTRIBUTED_KEY_ARGUMENTS else ""
            )
            routed_table = shard.table
            objects = scope.objects(database, routed_table)
            if not objects:
                msg = f"Managed shard {database}.{routed_table} is absent."
                raise ClickHouseClusterUnavailableError(msg)
            if writing and self.policy == "healthy_shards":
                objects = provision_missing(scope, database, routed_table, objects)
                defaults = getattr(self.config.ddl_defaults, "regular", None)
                facade = getattr(defaults, "distributed", None)
                facade_cluster = getattr(facade, "on_cluster", _MISSING)
                if facade_cluster is _MISSING:
                    facade_cluster = scope.cluster
                if (
                    isinstance(facade_cluster, str)
                    and self.scope(facade_cluster).cluster == scope.cluster
                    and not local_facade
                ):
                    provision_missing(scope, database, table, logical_objects)
            # Facades must agree where they are present, but reads use the
            # physical relation and do not require a facade on each replica.
            engine = objects[0].engine
        elif writing and self.policy == "healthy_shards":
            objects = provision_missing(scope, database, table, objects)
        if engine.lower() in {"view", "materializedview", "liveview", "windowview"}:
            scope.require_all(scope.reachable, "View dependencies cannot be degraded safely")
            return ManagedTableRoute(database, table, "local", engine)
        hosts = scope.usable(objects, f"{database}.{routed_table}", write=writing)
        if (
            self.policy == "healthy_shards"
            and writing
            and len(hosts) != len(scope.hosts)
            and not private_table(table)
            and not engine.lower().endswith("mergetree")
        ):
            msg = f"Unsupported ad hoc engine {engine!r}."
            raise ClickHouseClusterTopologyError(msg)
        replicated = engine.lower().startswith(("replicated", "shared"))
        multi_replica = any(
            sum(h.shard == host.shard for h in scope.hosts) > 1 for host in scope.hosts
        )
        all_replicas = private_table(table) or (not replicated and multi_replica)
        if writing:
            sharding_key = INSERT_KEY.get() or sharding_key
            self.insert_scope, self.insert_hosts = scope, hosts
            self.insert_all_replicas, self.insert_key = all_replicas, sharding_key
        if local_facade:
            return ManagedTableRoute(database, table, "local", "Distributed")
        return ManagedTableRoute(
            database,
            routed_table,
            "all_replicas" if all_replicas else "cluster",
            engine,
            sharding_key=sharding_key if writing else None,
        )

    def local_facade(self, database: str, table: str) -> list[Any]:
        # A facade may intentionally be deployed outside its physical routing cluster.
        rows = query_rows(
            self.client,
            "SELECT engine, engine_full, create_table_query, toString(uuid) FROM system.tables "  # noqa: S608 - escaped literals
            f"WHERE database = {sql_string_literal(database)} "
            f"AND name = {sql_string_literal(table)}",
        )
        if (
            not rows
            or len(rows[0]) != _DISTRIBUTED_KEY_ARGUMENTS
            or str(rows[0][0]).lower() != "distributed"
        ):
            return []
        host = Host(0, 0, 0, self.config.host, self.config.port, is_local=True)
        return [TableObject(host, *(str(item) for item in rows[0]))]

    def insertion_settings(self) -> dict[str, Any]:
        settings: dict[str, Any] = {
            "distributed_foreground_insert": 1,
            "async_insert": 0,
            "skip_unavailable_shards": 0,
        }
        scope = self.insert_scope
        if scope is None or self.policy != "healthy_shards":
            return settings
        if len(self.insert_hosts) == len(scope.hosts):
            return settings
        try:
            key = parse_one(self.insert_key, read=_RoutingClickHouse)
        except Exception as exc:
            msg = "Cannot verify the effective sharding key."
            raise ClickHouseClusterTopologyError(msg) from exc
        if not isinstance(key, exp.Anonymous) or key.name.lower() != "rand" or key.expressions:
            msg = "Degraded writes require the effective sharding key rand()."
            raise ClickHouseClusterUnavailableError(msg)
        candidates = sorted(
            {
                host.position if self.insert_all_replicas else host.shard
                for host in self.insert_hosts
            }
        )
        counter_key = ("ch_batches", self.config.connection_key, scope.cluster)
        with self.state.lock:
            counter = self.state.resources.get(counter_key, 0)
            self.state.resources[counter_key] = counter + 1
        settings.update(
            {
                "insert_shard_id": candidates[counter % len(candidates)],
                "parallel_distributed_insert_select": 0,
                "optimize_skip_unused_shards": 0,
                "optimize_distributed_group_by_sharding_key": 0,
            }
        )
        return settings

    def infer_routing(self, statement: exp.Expr) -> ChClusterRouting | None:
        configured = getattr(self.config, "cluster_routing", None)
        if configured is not None:
            return cast("ChClusterRouting", configured)
        for table in statement.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier) or table.db.lower() == "system":
                continue
            database = table.db or self.config.database
            if not database:
                continue
            rows = query_rows(
                self.client,
                "SELECT engine_full FROM system.tables "  # noqa: S608 - escaped literals or parsed SQL identifiers
                f"WHERE database = {sql_string_literal(database)} "
                f"AND name = {sql_string_literal(table.name)}",
            )
            if rows:
                shard = extract_clickhouse_distributed_shard_table(str(rows[0][0]), database)
                if shard is not None:
                    return ChClusterRouting(shard.cluster)
        return None
