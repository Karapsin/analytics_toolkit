from __future__ import annotations

# ruff: noqa: EM102, S608, TRY003
import re
from dataclasses import dataclass
from threading import RLock
from typing import Any, Literal

from analytics_toolkit.sql.backends.ch.metadata import (
    extract_clickhouse_distributed_shard_table,
)
from analytics_toolkit.sql.backends.transfer_stage import is_transfer_stage_identifier
from analytics_toolkit.sql.backends.utils import sql_string_literal
from analytics_toolkit.sql.connection.errors import ClickHouseClusterTopologyError

FACADE_METADATA_COLUMN_COUNT = 3
CLUSTER_TABLE_METADATA_COLUMN_COUNT = 5
CLUSTER_HOST_COLUMN_COUNT = 3
REPLICA_METADATA_COLUMN_COUNT = 5
SQL_QUOTE_PAIR_LENGTH = 2

RouteMode = Literal["cluster", "all_replicas", "local"]


@dataclass(frozen=True)
class ManagedTableRoute:
    database: str
    table: str
    mode: RouteMode
    engine: str | None = None


@dataclass(frozen=True)
class _ClusterObject:
    host_name: str
    engine: str
    engine_full: str
    create_table_query: str
    uuid: str


class _IncompleteClusterCoverageError(Exception):
    pass


class ManagedPairResolver:
    """Resolve named ClickHouse relations after validating their cluster topology."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._cache: dict[tuple[str, str, str], ManagedTableRoute] = {}
        self._lock = RLock()

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def resolve(self, *, cluster: str, database: str, table: str) -> ManagedTableRoute:
        key = (cluster, database, table)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            route = self._inspect_route(cluster=cluster, database=database, table=table)
            self._cache[key] = route
            return route

    def _inspect_route(
        self,
        *,
        cluster: str,
        database: str,
        table: str,
    ) -> ManagedTableRoute:
        if database.lower() == "system" or is_transfer_stage_identifier(table):
            self._validate_cluster_reachable(cluster)
            return ManagedTableRoute(database, table, "all_replicas")

        local = self._read_local_object(database=database, table=table)
        if local is None:
            raise ClickHouseClusterTopologyError(
                f"ClickHouse relation {database}.{table} is not visible on the routing host."
            )
        engine, engine_full, _create_query = local
        try:
            _objects, replicas_per_shard = self._read_consistent_cluster_object(
                cluster=cluster,
                database=database,
                table=table,
                allow_incomplete=engine.lower() == "distributed",
            )
        except _IncompleteClusterCoverageError:
            return ManagedTableRoute(database, table, "local", engine)

        if engine.lower() == "distributed":
            route = ManagedTableRoute(database, table, "local", engine)
            shard = extract_clickhouse_distributed_shard_table(engine_full, database)
            if shard is not None and shard.database == database and shard.table == f"{table}_shard":
                try:
                    shard_objects, shard_replicas_per_shard = self._read_consistent_cluster_object(
                        cluster=cluster,
                        database=shard.database,
                        table=shard.table,
                        allow_incomplete=True,
                    )
                except _IncompleteClusterCoverageError:
                    pass
                else:
                    shard_engine = shard_objects[0].engine
                    route = ManagedTableRoute(
                        shard.database,
                        shard.table,
                        self._route_mode(shard_engine, shard_replicas_per_shard),
                        shard_engine,
                    )
        elif engine.lower() in {"view", "materializedview", "liveview", "windowview"}:
            route = ManagedTableRoute(database, table, "local", engine)
        else:
            route = ManagedTableRoute(
                database,
                table,
                self._route_mode(engine, replicas_per_shard),
                engine,
            )
        return route

    def _read_local_object(
        self,
        *,
        database: str,
        table: str,
    ) -> tuple[str, str, str] | None:
        sql = (
            "SELECT engine, engine_full, create_table_query\n"
            "FROM system.tables\n"
            f"WHERE database = {sql_string_literal(database)}\n"
            f"  AND name = {sql_string_literal(table)}\n"
            "LIMIT 1"
        )
        try:
            rows = _query_rows(self._client, sql)
        except Exception as exc:
            raise ClickHouseClusterTopologyError(
                f"Could not inspect ClickHouse relation {database}.{table}."
            ) from exc
        if not rows:
            return None
        if len(rows[0]) < FACADE_METADATA_COLUMN_COUNT:
            raise ClickHouseClusterTopologyError(
                f"ClickHouse metadata for {database}.{table} is incomplete."
            )
        engine, engine_full, create_query = rows[0][:FACADE_METADATA_COLUMN_COUNT]
        if not all(isinstance(value, str) for value in (engine, engine_full, create_query)):
            raise ClickHouseClusterTopologyError(
                f"ClickHouse metadata for {database}.{table} has invalid values."
            )
        return engine, engine_full, create_query

    def _read_consistent_cluster_object(
        self,
        *,
        cluster: str,
        database: str,
        table: str,
        allow_incomplete: bool = False,
    ) -> tuple[list[_ClusterObject], dict[int, int]]:
        cluster_name = _resolve_cluster_name(self._client, cluster)
        expected = self._cluster_hosts(cluster_name)
        cluster_literal = sql_string_literal(cluster_name)
        sql = (
            "SELECT hostName(), engine, engine_full, "
            "create_table_query, toString(uuid)\n"
            f"FROM clusterAllReplicas({cluster_literal}, system, tables)\n"
            f"WHERE database = {sql_string_literal(database)}\n"
            f"  AND name = {sql_string_literal(table)}"
        )
        try:
            rows = _query_rows(self._client, sql)
        except Exception as exc:
            raise ClickHouseClusterTopologyError(
                f"Could not inspect {database}.{table} on every host of cluster {cluster_name!r}."
            ) from exc
        objects = [_cluster_object(row, database, table) for row in rows]
        actual_hosts = {item.host_name for item in objects}
        actual_count = len(actual_hosts)
        expected_count = sum(expected.values())
        if actual_count != expected_count or len(objects) != expected_count:
            detail = (
                f"ClickHouse relation {database}.{table} is present on "
                f"{actual_count}/{expected_count} routing-cluster hosts."
            )
            if allow_incomplete:
                raise _IncompleteClusterCoverageError(detail)
            raise ClickHouseClusterTopologyError(detail)

        engines = {item.engine.lower() for item in objects}
        definitions = {_normalized_definition(item) for item in objects}
        if len(engines) != 1 or len(definitions) != 1:
            detail = (
                f"ClickHouse relation {database}.{table} has inconsistent engine or DDL "
                f"on cluster {cluster_name!r}."
            )
            if allow_incomplete:
                raise _IncompleteClusterCoverageError(detail)
            raise ClickHouseClusterTopologyError(detail)
        if _is_replicated_engine(objects[0].engine):
            self._validate_replicas(
                cluster_name=cluster_name,
                database=database,
                table=table,
                expected=expected,
            )
        return objects, expected

    def _cluster_hosts(self, cluster_name: str) -> dict[int, int]:
        sql = (
            "SELECT shard_num, replica_num, host_name\n"
            "FROM system.clusters\n"
            f"WHERE cluster = {sql_string_literal(cluster_name)}"
        )
        try:
            rows = _query_rows(self._client, sql)
        except Exception as exc:
            raise ClickHouseClusterTopologyError(
                f"Could not inspect ClickHouse cluster {cluster_name!r}."
            ) from exc
        hosts: set[tuple[int, int]] = set()
        for row in rows:
            if len(row) < CLUSTER_HOST_COLUMN_COUNT:
                raise ClickHouseClusterTopologyError(
                    f"ClickHouse cluster metadata for {cluster_name!r} is incomplete."
                )
            hosts.add((int(row[0]), int(row[1])))
        if not hosts or len(hosts) != len(rows):
            raise ClickHouseClusterTopologyError(
                f"ClickHouse cluster {cluster_name!r} has no unique routing hosts."
            )
        counts: dict[int, int] = {}
        for shard_num, _replica_position in hosts:
            counts[shard_num] = counts.get(shard_num, 0) + 1
        return counts

    def _validate_cluster_reachable(self, cluster: str) -> None:
        cluster_name = _resolve_cluster_name(self._client, cluster)
        expected = self._cluster_hosts(cluster_name)
        try:
            reachable = _query_rows(
                self._client,
                "SELECT hostName() FROM clusterAllReplicas("
                f"{sql_string_literal(cluster_name)}, system, one)",
            )
        except Exception as exc:
            raise ClickHouseClusterTopologyError(
                f"Could not reach every host of ClickHouse cluster {cluster_name!r}."
            ) from exc
        actual = {str(row[0]) for row in reachable if row}
        expected_count = sum(expected.values())
        if len(actual) != expected_count or len(reachable) != expected_count:
            raise ClickHouseClusterTopologyError(
                f"Only {len(actual)}/{expected_count} hosts of ClickHouse cluster "
                f"{cluster_name!r} are reachable."
            )

    def _validate_replicas(
        self,
        *,
        cluster_name: str,
        database: str,
        table: str,
        expected: dict[int, int],
    ) -> None:
        sql = (
            "SELECT hostName(), zookeeper_path, replica_name, "
            "is_readonly, is_session_expired\n"
            "FROM clusterAllReplicas("
            f"{sql_string_literal(cluster_name)}, system, replicas)\n"
            f"WHERE database = {sql_string_literal(database)}\n"
            f"  AND table = {sql_string_literal(table)}"
        )
        try:
            rows = _query_rows(self._client, sql)
        except Exception as exc:
            raise ClickHouseClusterTopologyError(
                f"Could not validate replicas for {database}.{table}."
            ) from exc
        hosts: set[str] = set()
        names_by_path: dict[str, set[str]] = {}
        for row in rows:
            if len(row) < REPLICA_METADATA_COLUMN_COUNT:
                raise ClickHouseClusterTopologyError(
                    f"Replica metadata for {database}.{table} is incomplete."
                )
            hosts.add(str(row[0]))
            names = names_by_path.setdefault(str(row[1]), set())
            replica_name = str(row[2])
            if replica_name in names or bool(row[3]) or bool(row[4]):
                raise ClickHouseClusterTopologyError(
                    f"Replica topology for {database}.{table} is inconsistent or unhealthy."
                )
            names.add(replica_name)
        if (
            len(hosts) != sum(expected.values())
            or len(hosts) != len(rows)
            or sorted(len(names) for names in names_by_path.values()) != sorted(expected.values())
        ):
            raise ClickHouseClusterTopologyError(
                f"Replica topology for {database}.{table} does not match cluster {cluster_name!r}."
            )

    @staticmethod
    def _route_mode(
        engine: str,
        replicas_per_shard: dict[int, int],
    ) -> RouteMode:
        normalized = engine.lower()
        if _is_replicated_engine(engine) or normalized.startswith("shared"):
            return "cluster"
        if max(replicas_per_shard.values(), default=1) == 1:
            return "cluster"
        if normalized.endswith("mergetree"):
            return "all_replicas"
        raise ClickHouseClusterTopologyError(
            f"ClickHouse cluster routing does not know how to route engine {engine!r}."
        )


def _cluster_object(row: tuple[Any, ...], database: str, table: str) -> _ClusterObject:
    if len(row) < CLUSTER_TABLE_METADATA_COLUMN_COUNT:
        raise ClickHouseClusterTopologyError(
            f"Cluster metadata for {database}.{table} is incomplete."
        )
    return _ClusterObject(
        host_name=str(row[0]),
        engine=str(row[1]),
        engine_full=str(row[2]),
        create_table_query=str(row[3]),
        uuid=str(row[4]),
    )


def _normalized_definition(item: _ClusterObject) -> str:
    definition = " ".join((item.create_table_query or item.engine_full).split())
    definition = re.sub(r"\s+UUID\s+'[^']+'", "", definition, flags=re.IGNORECASE)
    if _is_replicated_engine(item.engine):
        definition = re.sub(
            r"(Replicated[A-Za-z]*MergeTree\s*\(\s*'[^']*'\s*,\s*)'[^']*'",
            r"\1'<replica>'",
            definition,
            flags=re.IGNORECASE,
        )
    return definition


def _is_replicated_engine(engine: str) -> bool:
    normalized = engine.strip().lower()
    return normalized.startswith("replicated") and normalized.endswith("mergetree")


def _resolve_cluster_name(client: Any, cluster: str) -> str:
    normalized = _strip_sql_wrapping_quotes(cluster.strip())
    match = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", normalized)
    if match is None:
        return normalized
    rows = _query_rows(client, f"SELECT getMacro({sql_string_literal(match.group(1))})")
    if not rows or not rows[0] or rows[0][0] is None:
        raise ClickHouseClusterTopologyError(
            f"Could not resolve ClickHouse cluster macro {normalized!r}."
        )
    resolved = str(rows[0][0]).strip()
    if not resolved:
        raise ClickHouseClusterTopologyError(
            f"Could not resolve ClickHouse cluster macro {normalized!r}."
        )
    return resolved


def _strip_sql_wrapping_quotes(value: str) -> str:
    if (
        len(value) < SQL_QUOTE_PAIR_LENGTH
        or value[0] != value[-1]
        or value[0] not in {"'", '"', "`"}
    ):
        return value
    inner = value[1:-1]
    if value[0] == "'":
        return inner.replace("''", "'")
    return inner


def _query_count(client: Any, sql: str) -> int:
    rows = _query_rows(client, sql)
    if not rows or not rows[0]:
        return 0
    return int(rows[0][0])


def _query_rows(client: Any, sql: str) -> list[tuple[Any, ...]]:
    result = client.query(sql)
    return list(getattr(result, "result_rows", None) or [])


__all__ = ["ManagedPairResolver", "ManagedTableRoute", "RouteMode"]
