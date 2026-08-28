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
CLUSTER_TABLE_METADATA_COLUMN_COUNT = 7
CLUSTER_HOST_COLUMN_COUNT = 3
REPLICA_METADATA_COLUMN_COUNT = 7
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
    shard_num: int
    replica_num: int
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
        objects = self._read_consistent_cluster_object(
            cluster=cluster,
            database=database,
            table=table,
        )

        if engine.lower() == "distributed":
            shard = extract_clickhouse_distributed_shard_table(engine_full, database)
            if shard is None or shard.database != database or shard.table != f"{table}_shard":
                return ManagedTableRoute(database, table, "local", engine)
            try:
                shard_objects = self._read_consistent_cluster_object(
                    cluster=cluster,
                    database=shard.database,
                    table=shard.table,
                    allow_incomplete=True,
                )
            except _IncompleteClusterCoverageError:
                return ManagedTableRoute(database, table, "local", engine)
            shard_engine = shard_objects[0].engine
            return ManagedTableRoute(
                shard.database,
                shard.table,
                self._route_mode(shard_engine, shard_objects),
                shard_engine,
            )

        if engine.lower() in {"view", "materializedview", "liveview", "windowview"}:
            return ManagedTableRoute(database, table, "local", engine)
        return ManagedTableRoute(
            database,
            table,
            self._route_mode(engine, objects),
            engine,
        )

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
    ) -> list[_ClusterObject]:
        cluster_name = _resolve_cluster_name(self._client, cluster)
        expected = self._cluster_hosts(cluster_name)
        cluster_literal = sql_string_literal(cluster_name)
        sql = (
            "SELECT _shard_num, _replica_num, hostName(), engine, engine_full, "
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
        actual = {(item.shard_num, item.replica_num) for item in objects}
        if actual != expected or len(objects) != len(expected):
            detail = (
                f"ClickHouse relation {database}.{table} is present on "
                f"{len(actual)}/{len(expected)} routing-cluster hosts."
            )
            if allow_incomplete:
                raise _IncompleteClusterCoverageError(detail)
            raise ClickHouseClusterTopologyError(detail)

        engines = {item.engine.lower() for item in objects}
        definitions = {_normalized_definition(item) for item in objects}
        if len(engines) != 1 or len(definitions) != 1:
            raise ClickHouseClusterTopologyError(
                f"ClickHouse relation {database}.{table} has inconsistent engine or DDL "
                f"on cluster {cluster_name!r}."
            )
        if _is_replicated_engine(objects[0].engine):
            self._validate_replicas(
                cluster_name=cluster_name,
                database=database,
                table=table,
                expected=expected,
            )
        return objects

    def _cluster_hosts(self, cluster_name: str) -> set[tuple[int, int]]:
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
        return hosts

    def _validate_cluster_reachable(self, cluster: str) -> None:
        cluster_name = _resolve_cluster_name(self._client, cluster)
        expected = self._cluster_hosts(cluster_name)
        try:
            reachable = _query_rows(
                self._client,
                "SELECT _shard_num, _replica_num, hostName() FROM clusterAllReplicas("
                f"{sql_string_literal(cluster_name)}, system, one)",
            )
        except Exception as exc:
            raise ClickHouseClusterTopologyError(
                f"Could not reach every host of ClickHouse cluster {cluster_name!r}."
            ) from exc
        actual = {
            (int(row[0]), int(row[1])) for row in reachable if len(row) >= CLUSTER_HOST_COLUMN_COUNT
        }
        if actual != expected:
            raise ClickHouseClusterTopologyError(
                f"Only {len(actual)}/{len(expected)} hosts of ClickHouse cluster "
                f"{cluster_name!r} are reachable."
            )

    def _validate_replicas(
        self,
        *,
        cluster_name: str,
        database: str,
        table: str,
        expected: set[tuple[int, int]],
    ) -> None:
        sql = (
            "SELECT _shard_num, _replica_num, hostName(), zookeeper_path, replica_name, "
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
        positions: set[tuple[int, int]] = set()
        paths_by_shard: dict[int, set[str]] = {}
        names_by_shard: dict[int, set[str]] = {}
        for row in rows:
            if len(row) < REPLICA_METADATA_COLUMN_COUNT:
                raise ClickHouseClusterTopologyError(
                    f"Replica metadata for {database}.{table} is incomplete."
                )
            shard_num, replica_num = int(row[0]), int(row[1])
            positions.add((shard_num, replica_num))
            paths_by_shard.setdefault(shard_num, set()).add(str(row[3]))
            names = names_by_shard.setdefault(shard_num, set())
            replica_name = str(row[4])
            if replica_name in names or bool(row[5]) or bool(row[6]):
                raise ClickHouseClusterTopologyError(
                    f"Replica topology for {database}.{table} is inconsistent or unhealthy."
                )
            names.add(replica_name)
        if positions != expected or any(len(paths) != 1 for paths in paths_by_shard.values()):
            raise ClickHouseClusterTopologyError(
                f"Replica topology for {database}.{table} does not match cluster {cluster_name!r}."
            )

    @staticmethod
    def _route_mode(engine: str, objects: list[_ClusterObject]) -> RouteMode:
        normalized = engine.lower()
        if _is_replicated_engine(engine) or normalized.startswith("shared"):
            return "cluster"
        replicas_per_shard: dict[int, int] = {}
        for item in objects:
            replicas_per_shard[item.shard_num] = replicas_per_shard.get(item.shard_num, 0) + 1
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
        shard_num=int(row[0]),
        replica_num=int(row[1]),
        host_name=str(row[2]),
        engine=str(row[3]),
        engine_full=str(row[4]),
        create_table_query=str(row[5]),
        uuid=str(row[6]),
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
