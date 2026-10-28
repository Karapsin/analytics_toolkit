"""Bounded, initiator-side topology probes for ClickHouse outage policies."""

from __future__ import annotations

import sys
import warnings
from contextlib import suppress
from dataclasses import dataclass
from html import escape
from importlib import import_module
from typing import Any

from analytics_toolkit.sql.backends.utils import sql_string_literal
from analytics_toolkit.sql.connection.errors import (
    ClickHouseClusterTopologyError,
    ClickHouseClusterUnavailableError,
    ClickHouseDegradedClusterWarning,
)
from analytics_toolkit.sql.execution.backend_options import (
    BackendOperationState,
    current_backend_query_label,
)
from analytics_toolkit.sql.execution.labels import apply_query_label

from .managed_routing import _ClusterObject, _normalized_definition, _resolve_cluster_name

_PROBE_FIELDS = 2
_HOST_FIELDS = 4
_TABLE_FIELDS = 5
_REPLICA_FIELDS = 5

PROBE_SETTINGS = {
    "skip_unavailable_shards": 1,
    "connect_timeout_with_failover_ms": 1000,
    "connections_with_failover_max_tries": 1,
    "max_execution_time": 5,
    "send_timeout": 5,
    "receive_timeout": 5,
}


@dataclass(frozen=True)
class Host:
    position: int
    shard: int
    replica: int
    name: str
    port: int
    is_local: bool = False

    @property
    def identity(self) -> str:
        return f"{self.name}:{self.port} (shard {self.shard}, replica {self.replica})"


@dataclass(frozen=True)
class TableObject:
    host: Host
    engine: str
    engine_full: str
    create_sql: str
    uuid: str


def query_rows(client: Any, sql: str, *, settings: dict[str, Any] | None = None) -> list[Any]:
    sql = apply_query_label(sql, current_backend_query_label())
    result = client.query(sql, **({"settings": settings} if settings is not None else {}))
    return list(result.result_rows)


def warn_degraded(
    state: BackendOperationState,
    config: Any,
    status: dict[str, Any],
    action: str,
) -> None:
    key = (
        config.connection_key,
        status["cluster"],
        status.get("table"),
        action,
        tuple(status["excluded_hosts"]),
    )
    with state.lock:
        if key in state.warned:
            return
        state.warned.add(key)
        state.observed_partial = True
        state.incomplete_aliases.add(config.connection_key)
        state.resources.setdefault("completion_callbacks", {})[config.connection_key] = (
            lambda success: _completion_banner(state, config.connection_key, succeeded=success)
        )
    message = (
        "!!! CLICKHOUSE AD HOC / DEGRADED MODE !!!\n"
        f"Alias: {config.connection_key}; cluster: {status['cluster']}; action: {action}\n"
        f"Table: {status.get('table') or '(cluster)'}\n"
        f"Using {len(status['selected_hosts'])}/{len(status['expected_hosts'])} hosts. "
        f"Excluded: {', '.join(status['excluded_hosts']) or '(none)'}\n"
        "READS MAY BE INCOMPLETE. WRITES USE ONLY THE SELECTED HEALTHY SHARDS.\n"
        "No historical data is moved. No DDL is queued for excluded hosts."
    )
    red = bool(getattr(sys.stderr, "isatty", lambda: False)())
    sys.stderr.write(("\033[1;31m" if red else "") + message + ("\033[0m" if red else "") + "\n")
    # Notebook presentation is optional; the stderr banner is always emitted.
    shell = sys.modules.get("IPython")
    get_shell = getattr(shell, "get_ipython", None)
    if get_shell is not None and get_shell() is not None:
        display = import_module("IPython.display")
        display.display(
            display.HTML(
                '<pre style="color:#c00;font-size:1.25em;font-weight:bold;'
                'border:4px solid #c00;padding:1em">' + escape(message) + "</pre>"
            )
        )
    warnings.warn(message, ClickHouseDegradedClusterWarning, stacklevel=3)


def _completion_banner(state: BackendOperationState, alias: str, *, succeeded: bool) -> None:
    entries = [item for item in state.statuses.values() if item["alias"] == alias]
    excluded = sorted({host for item in entries for host in item["excluded_hosts"]})
    message = (
        "!!! CLICKHOUSE AD HOC OPERATION " + ("FINISHED" if succeeded else "STOPPED") + " !!!\n"
        f"Alias: {alias}; excluded hosts observed: {', '.join(excluded) or '(none)'}\n"
        f"Complete operation coverage: {not state.partial}. "
        f"Recovery artifacts retained: {state.preserve_recovery}."
    )
    # Sink failures after an acknowledged write must not turn it into a retryable error.
    with suppress(OSError):
        red = bool(getattr(sys.stderr, "isatty", lambda: False)())
        sys.stderr.write(
            ("\033[1;31m" if red else "") + message + ("\033[0m" if red else "") + "\n"
        )


class ClusterScope:
    def __init__(
        self, client: Any, config: Any, cluster: str, state: BackendOperationState, policy: str
    ) -> None:
        self.client = client
        self.config = config
        self.state = state
        self.policy = policy
        self.cluster = _resolve_cluster_name(client, cluster)
        literal = sql_string_literal(self.cluster)
        rows = query_rows(
            client,
            "SELECT shard_num, replica_num, host_name, port, is_local FROM system.clusters "  # noqa: S608 - escaped literals or parsed SQL identifiers
            f"WHERE cluster = {literal} ORDER BY shard_num, replica_num",
        )
        try:
            self.hosts = [
                Host(
                    i + 1,
                    int(row[0]),
                    int(row[1]),
                    str(row[2]),
                    int(row[3]),
                    bool(row[4]) if len(row) > _HOST_FIELDS else str(row[2]) == config.host,
                )
                for i, row in enumerate(rows)
            ]
        except (IndexError, TypeError, ValueError) as exc:
            msg = "Incomplete configured cluster membership."
            raise ClickHouseClusterTopologyError(msg) from exc
        identities = {(host.shard, host.replica) for host in self.hosts}
        endpoints = {(host.name, host.port) for host in self.hosts}
        if (
            not self.hosts
            or len(identities) != len(self.hosts)
            or len(endpoints) != len(self.hosts)
        ):
            msg = f"Cluster {self.cluster!r} has no unique hosts."
            raise ClickHouseClusterTopologyError(msg)
        self.probe_settings: dict[str, Any] = dict(PROBE_SETTINGS)
        # Older supported servers do not have this setting. Never inherit a
        # profile's permissive catch-all exception mode on newer servers.
        modes = query_rows(
            client, "SELECT name FROM system.settings WHERE name = 'skip_unavailable_shards_mode'"
        )
        if modes:
            self.probe_settings["skip_unavailable_shards_mode"] = "unavailable_or_table_missing"
        reached = query_rows(
            client,
            f"SELECT _shard_num, hostName() FROM clusterAllReplicas({literal}, system, one)",  # noqa: S608 - escaped literals or parsed SQL identifiers
            settings=self.probe_settings,
        )
        try:
            positions = [int(row[0]) for row in reached]
            if any(len(row) < _PROBE_FIELDS or not row[1] for row in reached):
                msg = "Incomplete cluster reachability response."
                raise ClickHouseClusterTopologyError(msg)
        except (IndexError, TypeError, ValueError) as exc:
            msg = "Incomplete cluster reachability response."
            raise ClickHouseClusterTopologyError(msg) from exc
        if len(set(positions)) != len(positions) or any(
            position < 1 or position > len(self.hosts) for position in positions
        ):
            msg = "Unexpected cluster probe positions."
            raise ClickHouseClusterTopologyError(msg)
        self.reachable = [host for host in self.hosts if host.position in positions]
        if not self.reachable:
            msg = f"No hosts of {self.cluster!r} are reachable."
            raise ClickHouseClusterUnavailableError(msg)
        self.record(self.reachable)

    def record(self, selected: list[Host], table: str | None = None) -> dict[str, Any]:
        chosen = {host.position for host in selected}
        status = {
            "alias": self.config.connection_key,
            "cluster": self.cluster,
            "table": table,
            "policy": self.policy,
            "complete": len(selected) == len(self.hosts),
            "expected_hosts": [host.identity for host in self.hosts],
            "selected_hosts": [host.identity for host in selected],
            "excluded_hosts": [host.identity for host in self.hosts if host.position not in chosen],
            "selected_shards": sorted({host.shard for host in selected}),
        }
        with self.state.lock:
            self.state.statuses[(self.config.connection_key, f"{self.cluster}:{table or ''}")] = (
                status
            )
        return status

    def require_all(self, selected: list[Host], reason: str) -> None:
        if len(selected) != len(self.hosts):
            missing = [host.identity for host in self.hosts if host not in selected]
            msg = (
                f"{reason}: cluster {self.cluster!r} is incomplete; "
                f"excluded: {', '.join(missing)}. "
                "Use ch_unavailable_shards='healthy_shards' only for explicit ad hoc work."
            )
            raise ClickHouseClusterUnavailableError(msg)

    def objects(self, database: str, table: str) -> list[TableObject]:
        sql = (
            "SELECT _shard_num, engine, engine_full, create_table_query, toString(uuid) "  # noqa: S608 - escaped literals or parsed SQL identifiers
            f"FROM clusterAllReplicas({sql_string_literal(self.cluster)}, system, tables) "
            f"WHERE database = {sql_string_literal(database)} "
            f"AND name = {sql_string_literal(table)}"
        )
        rows = query_rows(self.client, sql, settings=self.probe_settings)
        objects: list[TableObject] = []
        for row in rows:
            if len(row) < _TABLE_FIELDS:
                msg = f"Incomplete metadata for {database}.{table}."
                raise ClickHouseClusterTopologyError(msg)
            position = int(row[0])
            if position < 1 or position > len(self.hosts):
                msg = "Unexpected table metadata position."
                raise ClickHouseClusterTopologyError(msg)
            objects.append(TableObject(self.hosts[position - 1], *(str(item) for item in row[1:5])))
        if len({item.host.position for item in objects}) != len(objects):
            msg = "Duplicate table metadata positions."
            raise ClickHouseClusterTopologyError(msg)
        definitions = {
            _normalized_definition(
                _ClusterObject(
                    item.host.identity,
                    item.engine,
                    item.engine_full,
                    item.create_sql,
                    item.uuid,
                )
            )
            for item in objects
        }
        if len(definitions) > 1:
            msg = f"Conflicting schema for {database}.{table}."
            raise ClickHouseClusterTopologyError(msg)
        return objects

    def _require_ready_replicas(self, selected: list[Host], table: str) -> None:
        ready_shards = {host.shard for host in selected}
        if all(host in selected or host.shard not in ready_shards for host in self.reachable):
            return
        status = self.record(selected, table)
        if self.policy == "healthy_shards":
            warn_degraded(self.state, self.config, status, "blocked table-missing replica")
        msg = (
            f"Reachable replicas are missing {table}; cluster routing cannot safely "
            "select only table-ready replicas. Reconcile their schemas before retrying."
        )
        raise ClickHouseClusterUnavailableError(msg)

    def usable(self, objects: list[TableObject], table: str, *, write: bool = False) -> list[Host]:  # noqa: C901 - keep the safety decision and submission in one boundary
        if not objects:
            msg = f"No table-ready hosts for {table}."
            raise ClickHouseClusterUnavailableError(msg)
        selected = [item.host for item in objects if item.host in self.reachable]
        replicated = objects[0].engine.lower().startswith(("replicated", "shared"))
        if replicated:
            self._require_ready_replicas(selected, table)
            replica_rows = query_rows(
                self.client,
                "SELECT _shard_num, zookeeper_path, replica_name, is_readonly, is_session_expired "  # noqa: S608 - escaped literals or parsed SQL identifiers
                f"FROM clusterAllReplicas({sql_string_literal(self.cluster)}, system, replicas) "
                f"WHERE database = {sql_string_literal(table.split('.', 1)[0])} "
                f"AND table = {sql_string_literal(table.split('.', 1)[1])}",
                settings=self.probe_settings,
            )
            # Shared engines have different coordination metadata.
            if objects[0].engine.lower().startswith("replicated"):
                by_position = {
                    int(row[0]): row for row in replica_rows if len(row) >= _REPLICA_FIELDS
                }
                paths: dict[str, int] = {}
                shard_paths: dict[int, str] = {}
                names: set[tuple[str, str]] = set()
                for host in selected:
                    row = by_position.get(host.position)
                    if row is None:
                        msg = f"Missing replica metadata for {table}."
                        raise ClickHouseClusterTopologyError(msg)
                    path, replica = str(row[1]), str(row[2])
                    if (
                        (path in paths and paths[path] != host.shard)
                        or (host.shard in shard_paths and shard_paths[host.shard] != path)
                        or (path, replica) in names
                    ):
                        msg = f"Unsafe replica identities for {table}."
                        raise ClickHouseClusterTopologyError(msg)
                    paths[path] = host.shard
                    shard_paths[host.shard] = path
                    names.add((path, replica))
                usable = [
                    host
                    for host in selected
                    if not (
                        bool(by_position[host.position][4])
                        or (write and bool(by_position[host.position][3]))
                    )
                ]
                if usable != selected:
                    msg = (
                        f"No usable route for {table}: a reachable replica has unhealthy "
                        "replication metadata; cluster routing cannot exclude it safely."
                    )
                    raise ClickHouseClusterUnavailableError(msg)
        if not selected:
            msg = f"No usable hosts for {table}."
            raise ClickHouseClusterUnavailableError(msg)
        status = self.record(selected, table)
        if replicated:
            status["host_coverage_complete"] = status["complete"]
            status["complete"] = {host.shard for host in selected} == {
                host.shard for host in self.hosts
            }
        if self.policy == "stop":
            expected_shards = {host.shard for host in self.hosts}
            if not replicated or {host.shard for host in selected} != expected_shards:
                self.require_all(selected, f"Table {table}")
        elif not status["complete"]:
            warn_degraded(self.state, self.config, status, "append" if write else "read")
        return selected
