"""Replicated endpoint state with grouped aliases and independently lagging peers."""

from __future__ import annotations

from copy import deepcopy
from uuid import UUID

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import DropScopeState
from tests.sql._support.ch_lifecycle import LifecycleConnection, install_lifecycle
from tests.sql._support.reconfigure_conversion_scope import _name, _scope_sql


class ReplicaLifecycleState(DropScopeState):
    def __init__(self):
        self.replication_pending = {}
        self.keeper_domains = {}
        self.replica_macros = {}
        self.replica_defaults = ("/tables/{uuid}/{shard}", "{replica}")
        self.syncs = []
        self.queue_rows = {}
        self.groups = {}
        super().__init__()
        self.connection_class = ReplicaLifecycleConnection
        self.clusters.update(
            storage=(self.coordinator, self.remote),
            storage_alias=(self.remote, self.coordinator),
            split=(self.coordinator, self.remote),
        )
        self.groups = {
            name: tuple((1, replica, host) for replica, host in enumerate(hosts, 1))
            for name, hosts in self.clusters.items()
        }
        self.groups["split"] = ((1, 1, self.coordinator), (2, 1, self.remote))
        self.preferred_read_replica = self.remote
        original = deepcopy(self.host_tables[self.coordinator]["analytics.events_shard"].rows)
        foreign = parse_create_table(
            "CREATE TABLE analytics.unrelated (id UInt64) ENGINE=MergeTree ORDER BY id",
            "analytics.unrelated",
        )
        self._create(self.coordinator, foreign, original)
        for host in (self.coordinator, self.remote):
            self.host_tables[host].pop("analytics.events_shard")
            physical = parse_create_table(
                "CREATE TABLE analytics.events_shard (id UInt64) "
                "ENGINE=ReplicatedMergeTree('/tables/{table}/{shard}', '{replica}') ORDER BY id",
                "analytics.events_shard",
            )
            self._create(host, physical, [(11,), (12,), (13,)])
            self.host_tables[host].pop(self.source_table, None)
            facade = parse_create_table(
                "CREATE TABLE analytics.events (id UInt64) "
                "ENGINE=Distributed('storage','analytics','events_shard',rand())",
                self.source_table,
            )
            self._create(host, facade)
        self.created.clear()

    def selected_hosts(self, cluster, *, writing=False):
        selected = {}
        for shard, _replica, host in self.groups[cluster]:
            if shard not in selected or (not writing and host == self.preferred_read_replica):
                selected[shard] = host
        return tuple(selected.values())

    def replication_identity(self, host, name):
        value = self.host_tables[host][name]
        if hasattr(value, "_replication_identity"):
            return value._replication_identity
        return self._expanded_replication_identity(host, name)

    def _expanded_replication_identity(self, host, name):
        value = self.host_tables[host][name]
        if not value.engine.casefold().startswith("replicated"):
            return None
        engine = value.create.find(exp.EngineProperty).this
        arguments = list(engine.expressions)
        path, replica = (
            (arguments[0].this, arguments[1].this) if len(arguments) >= 2 else self.replica_defaults
        )
        database, relation = name.split(".", 1)
        macros = {
            "database": database,
            "table": relation,
            "uuid": str(value.uuid),
            "shard": "1",
            "replica": host,
            **self.replica_macros.get(host, {}),
        }
        return (
            self.keeper_domains.get(host, "shared"),
            str(path).format_map(macros),
            str(replica).format_map(macros),
        )

    def _create(self, host, create, rows=()):
        super()._create(host, create, rows)
        name = _name(create.this.this)
        value = self.host_tables[host][name]
        native_uuid = create.find(exp.UuidProperty)
        if native_uuid is not None:
            value._resource_uuid = UUID(native_uuid.this.name)
        value._replication_identity = self._expanded_replication_identity(host, name)
        identity = self.replication_identity(host, name)
        if identity is None or rows:
            return
        peers = [
            peer
            for endpoint, tables in self.host_tables.items()
            for relation, peer in tables.items()
            if (endpoint, relation) != (host, name)
            and (candidate := self.replication_identity(endpoint, relation)) is not None
            and candidate[:2] == identity[:2]
        ]
        if peers:
            value.rows = deepcopy(peers[0].rows)

    def _read(self, host, name):
        value = self.host_tables[host][name]
        if value.route is None:
            return value.rows
        cluster, physical = value.route
        return [row for peer in self.selected_hosts(cluster) for row in self._read(peer, physical)]

    def insert_rows(self, host, table, rows):
        name = _name(table)
        value = self.host_tables[host][name]
        incoming = deepcopy(list(rows))
        if value.route is not None:
            cluster, physical = value.route
            peers = self.selected_hosts(cluster, writing=True)
            for row in incoming:
                self.insert_rows(peers[int(row[0]) % len(peers)], physical, [row])
            return
        value.rows.extend(incoming)
        identity = self.replication_identity(host, name)
        if identity is not None:
            for endpoint, tables in self.host_tables.items():
                for relation in tables:
                    candidate = self.replication_identity(endpoint, relation)
                    if (
                        (endpoint, relation) != (host, name)
                        and candidate is not None
                        and candidate[:2] == identity[:2]
                    ):
                        self.replication_pending.setdefault((endpoint, relation), []).extend(
                            deepcopy(incoming)
                        )

    def sync(self, hosts, name):
        groups = {
            self.replication_identity(host, name)[:2]
            for host in hosts
            if name in self.host_tables[host]
        }
        for (host, relation), rows in tuple(self.replication_pending.items()):
            identity = self.replication_identity(host, relation)
            if identity is not None and identity[:2] in groups:
                self.host_tables[host][relation].rows.extend(rows)
                self.replication_pending.pop((host, relation))

    def drain_replication(self):
        for (host, relation), rows in tuple(self.replication_pending.items()):
            if relation in self.host_tables[host]:
                self.host_tables[host][relation].rows.extend(rows)
            self.replication_pending.pop((host, relation))

    def _metadata(self, name, hosts):
        if name in {"system.replicas", "system.zookeeper"}:
            return self._replica_metadata(name, hosts)
        if name == "system.distribution_queue":
            return [
                {
                    "_shard_num": position,
                    "database": table.split(".", 1)[0],
                    "table": table.split(".", 1)[1],
                    "data_files": len(rows),
                    "broken_data_files": 0,
                    "bytes": len(rows) * 8,
                    "error_count": 0,
                }
                for position, host in enumerate(hosts, 1)
                for (endpoint, table), rows in self.queue_rows.items()
                if endpoint == host and rows
            ]
        if name == "system.macros":
            return [
                {"_shard_num": position, "macro": macro, "substitution": value}
                for position, host in enumerate(hosts, 1)
                for macro, value in {
                    "shard": "1",
                    "replica": host,
                    **self.replica_macros.get(host, {}),
                }.items()
            ]
        records = super()._metadata(name, hosts)
        if name == "system.clusters" and self.groups:
            membership = {
                (cluster, self.host_endpoints[host][0]): (shard, replica)
                for cluster, members in self.groups.items()
                for shard, replica, host in members
            }
            for record in records:
                record["shard_num"], record["replica_num"] = membership[
                    record["cluster"], record["host_name"]
                ]
        return records

    def _replica_metadata(self, name, hosts):
        rows = []
        for position, observer in enumerate(hosts, 1):
            for host, tables in self.host_tables.items():
                for table in tables:
                    identity = self.replication_identity(host, table)
                    if identity is None:
                        continue
                    domain, path, replica = identity
                    if name == "system.replicas" and host == observer:
                        database, relation = table.split(".", 1)
                        rows.append(
                            {
                                "_shard_num": position,
                                "database": database,
                                "table": relation,
                                "zookeeper_path": path,
                                "replica_name": replica,
                                "zookeeper_name": "",
                                "is_readonly": 0,
                                "is_session_expired": 0,
                            }
                        )
                    elif name == "system.zookeeper" and domain == self.keeper_domains.get(
                        observer, "shared"
                    ):
                        rows.append(
                            {
                                "_shard_num": position,
                                "path": f"{path}/replicas/{replica}",
                                "name": "is_active",
                                "value": f"UUID_'{self.server_identities[host][0]}'",
                                "ephemeralOwner": tuple(self.host_tables).index(host) + 1,
                                "zookeeperName": "",
                            }
                        )
        return rows


class ReplicaLifecycleConnection(LifecycleConnection):
    def _reference(self, target):
        function = target.this if isinstance(target, (exp.Table, exp.Schema)) else target
        if isinstance(function, exp.Anonymous) and function.name.casefold() == "cluster":
            cluster, database, relation, _trailing = cluster_arguments(function, "analytics")
            hosts = self.state.selected_hosts(cluster, writing=getattr(self, "_writing", False))
            return hosts, f"{database}.{relation}"
        return super()._reference(target)

    def _insert(self, target, rows, columns=None):
        self._writing = True
        try:
            return super()._insert(target, rows, columns)
        finally:
            self._writing = False

    def command(self, statement, settings=None):
        scope, body = _scope_sql(strip_query_label(statement))
        prefix = "SYSTEM SYNC REPLICA "
        if not body.upper().startswith(prefix):
            return super().command(statement, settings=settings)
        table = parse_one(body[len(prefix) :], read=ClickHouseDialect, into=exp.Table)
        name = _name(table)
        hosts = self.state.clusters[scope] if scope is not None else (self.host,)
        self.state.commands.append(statement)
        self.state.command_settings.append(deepcopy(settings))
        for host in hosts:
            self.state.host_commands[host].append(statement)
        self.state.syncs.append((name, scope, len(self.state.commands)))
        self.state.sync(hosts, name)
        return None


def install_replica_lifecycle(monkeypatch, write_sql_connections, *, driver):
    install_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    state = ReplicaLifecycleState()
    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", state.open)
    return state
