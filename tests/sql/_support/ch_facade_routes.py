"""Each facade server resolves its own cluster aliases and macros."""

from __future__ import annotations

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from sqlglot import exp

from tests.sql._support.ch_replica_lifecycle import (
    ReplicaLifecycleConnection,
    ReplicaLifecycleState,
    install_replica_lifecycle,
)


class FacadeRouteState(ReplicaLifecycleState):
    def __init__(self):
        super().__init__()
        self.connection_class = FacadeRouteConnection
        self.clusters.update(
            desired=self.clusters["storage"], remote_alias=self.clusters["storage"]
        )
        self.groups.update(desired=self.groups["storage"], remote_alias=self.groups["storage"])
        self.route_members = {}
        self.facade_macros = {
            self.coordinator: {"route": "desired"},
            self.remote: {"route": "remote_alias"},
        }

    def resolve_route(self, origin, cluster):
        return self.facade_macros[origin][cluster[1:-1]] if cluster.startswith("{") else cluster

    def members_on(self, origin, cluster):
        name = self.resolve_route(origin, cluster)
        return self.route_members.get((origin, name), self.groups[name])

    def hosts_on(self, origin, cluster, *, writing=False):
        selected = {}
        for shard, _replica, host in self.members_on(origin, cluster):
            if shard not in selected or (not writing and host == self.preferred_read_replica):
                selected[shard] = host
        return tuple(selected.values())

    def _read(self, host, name):
        value = self.host_tables[host][name]
        if value.route is None:
            return value.rows
        cluster, physical = value.route
        return [row for peer in self.hosts_on(host, cluster) for row in self._read(peer, physical)]

    def insert_rows(self, host, table, rows):
        value = self.host_tables[host][table]
        if value.route is None:
            return super().insert_rows(host, table, rows)
        cluster, physical = value.route
        peers = self.hosts_on(host, cluster, writing=True)
        for row in rows:
            super().insert_rows(peers[int(row[0]) % len(peers)], physical, [row])
        return None

    def _metadata(self, name, hosts):
        if name != "system.clusters":
            return super()._metadata(name, hosts)
        return [
            {
                "_shard_num": position,
                "cluster": cluster,
                "shard_num": shard,
                "replica_num": replica,
                "host_name": self.host_endpoints[host][0],
                "host_address": self.host_endpoints[host][1],
                "port": self.host_endpoints[host][2],
                "is_local": int(host == origin),
            }
            for position, origin in enumerate(hosts, 1)
            for cluster in self.groups
            for shard, replica, host in self.members_on(origin, cluster)
        ]


class FacadeRouteConnection(ReplicaLifecycleConnection):
    def _reference(self, target):
        function = target.this if isinstance(target, (exp.Table, exp.Schema)) else target
        if isinstance(function, exp.Anonymous) and function.name.casefold() in {
            "cluster",
            "clusterallreplicas",
        }:
            cluster, database, relation, _trailing = cluster_arguments(function, "analytics")
            hosts = (
                self.state.hosts_on(self.host, cluster, writing=getattr(self, "_writing", False))
                if function.name.casefold() == "cluster"
                else tuple(member[2] for member in self.state.members_on(self.host, cluster))
            )
            return hosts, f"{database}.{relation}"
        return super()._reference(target)

    def _source(self, select):
        records, columns = super()._source(select)
        calls = [
            function
            for function in select.find_all(exp.Anonymous)
            if function.name.casefold() == "getmacro"
        ]
        if select.args.get("from_") is None and calls:
            assert len(calls) == 1
            macro = calls[0].expressions[0].this
            records[0]["getMacro"] = self.state.facade_macros[self.host][macro]
        return records, columns


def install_facade_routes(monkeypatch, write_sql_connections, *, driver):
    install_replica_lifecycle(monkeypatch, write_sql_connections, driver=driver)
    state = FacadeRouteState()
    monkeypatch.setattr(get_backend_adapter("ch"), "open_connection", state.open)
    return state
