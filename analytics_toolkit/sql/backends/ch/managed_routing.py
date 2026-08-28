from __future__ import annotations

import re
from dataclasses import dataclass
from threading import RLock
from typing import Any, Literal

from analytics_toolkit.sql.backends.ch.metadata import (
    extract_clickhouse_distributed_shard_table,
)
from analytics_toolkit.sql.backends.utils import sql_string_literal

FACADE_METADATA_COLUMN_COUNT = 2
SQL_QUOTE_PAIR_LENGTH = 2


@dataclass(frozen=True)
class ManagedTableRoute:
    database: str
    table: str
    mode: Literal["cluster", "local"]


class ManagedPairResolver:
    def __init__(self, client: Any) -> None:
        self._client = client
        self._cache: dict[tuple[str, str, str], ManagedTableRoute | None] = {}
        self._lock = RLock()

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def resolve(
        self,
        *,
        cluster: str,
        database: str,
        table: str,
    ) -> ManagedTableRoute | None:
        key = (cluster, database, table)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            route = self._inspect_route(
                cluster=cluster,
                database=database,
                table=table,
            )
            self._cache[key] = route
            return route

    def _inspect_route(
        self,
        *,
        cluster: str,
        database: str,
        table: str,
    ) -> ManagedTableRoute | None:
        facade = self._read_facade(database=database, table=table)
        if facade is None:
            return None
        engine, engine_full = facade
        if engine.lower() != "distributed":
            return None

        shard = extract_clickhouse_distributed_shard_table(engine_full, database)
        if shard is None or shard.database != database or shard.table != f"{table}_shard":
            return None

        if self._has_full_cluster_coverage(
            cluster=cluster,
            database=shard.database,
            table=shard.table,
        ):
            return ManagedTableRoute(shard.database, shard.table, "cluster")
        return ManagedTableRoute(database, table, "local")

    def _read_facade(self, *, database: str, table: str) -> tuple[str, str] | None:
        sql = (
            "SELECT engine, engine_full\n"
            "FROM system.tables\n"
            f"WHERE database = {sql_string_literal(database)}\n"
            f"  AND name = {sql_string_literal(table)}\n"
            "LIMIT 1"
        )
        try:
            rows = _query_rows(self._client, sql)
        except Exception:  # noqa: BLE001 -- metadata inspection must fail closed.
            return None
        if not rows or len(rows[0]) < FACADE_METADATA_COLUMN_COUNT:
            return None
        engine, engine_full = rows[0][:FACADE_METADATA_COLUMN_COUNT]
        if not isinstance(engine, str) or not isinstance(engine_full, str):
            return None
        return engine, engine_full

    def _has_full_cluster_coverage(
        self,
        *,
        cluster: str,
        database: str,
        table: str,
    ) -> bool:
        try:
            cluster_name = _resolve_cluster_name(self._client, cluster)
            cluster_literal = sql_string_literal(cluster_name)
            configured_hosts = _query_count(
                self._client,
                f"SELECT count()\nFROM system.clusters\nWHERE cluster = {cluster_literal}",
            )
            reachable_hosts = _query_count(
                self._client,
                f"SELECT count()\nFROM clusterAllReplicas({cluster_literal}, system, one)",
            )
            expected_hosts = max(configured_hosts, reachable_hosts)
            visible_tables = _query_count(
                self._client,
                "SELECT count()\n"
                f"FROM clusterAllReplicas({cluster_literal}, system, tables)\n"
                f"WHERE database = {sql_string_literal(database)}\n"
                f"  AND name = {sql_string_literal(table)}",
            )
        except Exception:  # noqa: BLE001 -- topology inspection must fail closed.
            return False
        return expected_hosts > 0 and visible_tables >= expected_hosts


def _resolve_cluster_name(client: Any, cluster: str) -> str:
    normalized = _strip_sql_wrapping_quotes(cluster.strip())
    match = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", normalized)
    if match is None:
        return normalized
    rows = _query_rows(
        client,
        f"SELECT getMacro({sql_string_literal(match.group(1))})",
    )
    if not rows or not rows[0] or rows[0][0] is None:
        message = f"Could not resolve ClickHouse cluster macro {normalized!r}."
        raise ValueError(message)
    resolved = str(rows[0][0]).strip()
    if not resolved:
        message = f"Could not resolve ClickHouse cluster macro {normalized!r}."
        raise ValueError(message)
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


__all__ = ["ManagedPairResolver", "ManagedTableRoute"]
