"""Raw metadata response faults behind the real reconfiguration outage wrapper."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.metadata import extract_clickhouse_macro_name
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from sqlglot import exp, parse_one

from tests.sql._support.reconfigure_conversion_scope import _project
from tests.sql._support.reconfigure_pair_scope import (
    PairScopeClient,
    configure_pair_scope,
    connect_pair_scope,
)


def projection(select):
    return tuple(item.name.casefold() for item in select.expressions)


def query_identity(select):
    source = select.args.get("from_")
    table = source.this if source is not None else None
    if not isinstance(table, exp.Table):
        return None, None, None
    if isinstance(table.this, exp.Anonymous):
        cluster, database, relation, _trailing = cluster_arguments(table.this, "analytics")
        return cluster, database, relation
    return None, table.db or "analytics", table.name


class MetadataFaultClient(PairScopeClient):
    def __init__(self, transform):
        super().__init__(managed_remote_facade=True)
        self.transform = transform
        self.fault_hits = []
        self.node_clusters = {host: dict(self.clusters) for host in self.host_tables}
        self.node_macros = {host: {"cluster": "core"} for host in self.host_tables}

    def query(self, statement, settings=None):
        result = super().query(statement, settings=settings)
        if statement.startswith(("SHOW ", "EXISTS ")):
            return result
        select = parse_one(statement, read=ClickHouseDialect)
        if not isinstance(select, exp.Select):
            return result
        replacement = self.transform(select, deepcopy(result.result_rows))
        if replacement is None:
            return result
        self.fault_hits.append((query_identity(select), projection(select)))
        return SimpleNamespace(result_rows=replacement)

    def _resolved_cluster(self, host, cluster):
        macro = extract_clickhouse_macro_name(cluster)
        return self.node_macros[host][macro] if macro is not None else cluster

    def _read(self, host, table):
        value = self.host_tables[host][table]
        if value.route is None:
            return value.rows
        cluster, physical = value.route
        cluster = self._resolved_cluster(host, cluster)
        return [
            row
            for endpoint in self.node_clusters[host][cluster]
            for row in self._read(endpoint, physical)
        ]

    def _select(self, select):
        source = select.args.get("from_")
        if source is None:
            record = {**self._host_record(self.coordinator), "currentDatabase": "analytics"}
            return [
                tuple(
                    self.node_macros[self.coordinator][item.expressions[0].name]
                    if isinstance(item, exp.Anonymous) and item.name.casefold() == "getmacro"
                    else _project(item, record)
                    for item in select.expressions
                )
            ]
        table = source.this
        if isinstance(table, exp.Table) and isinstance(table.this, exp.Anonymous):
            function = table.this
            cluster, _database, _relation, _trailing = cluster_arguments(function, "analytics")
            resolved = self._resolved_cluster(self.coordinator, cluster)
            if resolved != cluster:
                select = select.copy()
                function = select.args["from_"].this.this
                function.set(
                    "expressions", [exp.Literal.string(resolved), *function.expressions[1:]]
                )
        return super()._select(select)

    def _metadata(self, name, hosts):
        if name == "system.macros":
            return [
                {"_shard_num": position, "macro": macro, "substitution": value}
                for position, host in enumerate(hosts, 1)
                for macro, value in self.node_macros[host].items()
            ]
        if name == "system.clusters":
            return [
                {
                    "_shard_num": position,
                    "cluster": cluster,
                    "host_name": self.host_endpoints[endpoint][0],
                    "host_address": self.host_endpoints[endpoint][1],
                    "port": self.host_endpoints[endpoint][2],
                    "shard_num": index,
                    "replica_num": 1,
                    "is_local": int(endpoint == origin),
                }
                for position, origin in enumerate(hosts, 1)
                for cluster, endpoints in self.node_clusters[origin].items()
                for index, endpoint in enumerate(endpoints, 1)
            ]
        return super()._metadata(name, hosts)


def prepare_fault(monkeypatch, write_sql_connections, transform):
    configure_pair_scope(write_sql_connections, "core")
    raw = MetadataFaultClient(transform)
    connect_pair_scope(monkeypatch, raw)
    return raw


def mutate_facade(*, dry_run=False):
    return sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        ch_sharding_key="id + 1",
        ch_distributed_on_cluster="core",
        retry_cnt=1,
        timeout_increment=0,
        dry_run=dry_run,
        return_metadata=True,
    )


def assert_rejected_unchanged(raw, before):
    assert raw.fault_hits
    assert_no_mutation(raw, before)


def assert_no_mutation(raw, before):
    assert len(raw.wrappers) == 1
    assert isinstance(raw.wrappers[0], OutageClient)
    assert raw.commands == []
    assert raw.snapshot() == before
    assert raw.closed
