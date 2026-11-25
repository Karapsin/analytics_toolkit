"""Independent physical rows behind native Distributed replica selection.

The Distributed engine reads one replica per shard with max_parallel_replicas=1.
Its internal_replication setting sends inserts to one healthy replica (true), or
to every replica (false). This fixture pins the healthy native selection to the
coordinator and models both documented write modes without automatic routing:
https://clickhouse.com/docs/engines/table-engines/special/distributed
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace

from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.execution.observation import observed_connection
from sqlglot import exp, parse_one

from tests.sql._support.reconfigure_conversion_scope import _name, _project
from tests.sql._support.reconfigure_pair_scope import (
    PairScopeClient,
    _metadata_matches,
    _metadata_project,
    connect_pair_scope,
)


def configure_independent_replicas(write_sql_connections):
    path = write_sql_connections(
        {
            "ch": {
                "type": "ch",
                "driver": "native",
                "host": "coordinator",
                "port": 9000,
                "user": "user",
                "password": "password",
                "database": "analytics",
                "settings": {
                    "max_parallel_replicas": 1,
                    "prefer_localhost_replica": 1,
                    "load_balancing": "in_order",
                },
                "ddl_defaults": {
                    "regular": {
                        "create_distributed_pair": True,
                        "shard": {"engine": "MergeTree", "on_cluster": "storage"},
                        "distributed": {
                            "engine_template": (
                                "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                            ),
                            "cluster": "storage",
                            "on_cluster": None,
                            "sharding_key": "rand()",
                        },
                    }
                },
            }
        }
    )
    set_connections_path(path)


class IndependentReplicaClient(PairScopeClient):
    source_physical = "analytics.events_shard"

    def __init__(self, *, internal_replication, replicas=2):
        super().__init__()
        self.internal_replication = internal_replication
        self.clusters = {
            "storage": (self.coordinator, self.remote)[:replicas],
        }
        self.host_tables[self.remote].pop(self.source_table, None)
        physical = parse_create_table(
            "CREATE TABLE analytics.events_shard (id UInt64) ENGINE=MergeTree ORDER BY id",
            self.source_physical,
        )
        self._create(self.coordinator, physical, [(1,), (2,), (3,)])
        if replicas == 2:
            self.host_tables[self.remote][self.source_physical].rows = deepcopy(
                self.remote_original
            )
        else:
            self.host_tables[self.remote].pop(self.source_physical)
        self.native_reads = []
        self.native_insert_fanout = []
        self.worker_commands = []
        self.worker_connections = []
        self.created.clear()

    def physical_counters(self):
        return {
            host: Counter(self.host_tables[host][self.source_physical].rows)
            for host in self.clusters["storage"]
        }

    def _native_replica(self, cluster, origin):
        hosts = self.clusters[cluster]
        return origin if origin in hosts else hosts[0]

    def _read(self, host, table):
        value = self.host_tables[host][table]
        if value.route is None:
            return value.rows
        cluster, physical = value.route
        selected = self._native_replica(cluster, host)
        self.native_reads.append((table, selected, physical))
        return self._read(selected, physical)

    def insert_rows(self, host, table, rows):
        value = self.host_tables[host][_name(table)]
        if value.route is None:
            value.rows.extend(deepcopy(list(rows)))
            return
        cluster, physical = value.route
        selected = self._native_replica(cluster, host)
        targets = (selected,) if self.internal_replication else self.clusters[cluster]
        self.native_insert_fanout.append((table, targets, deepcopy(list(rows))))
        for destination in targets:
            self.insert_rows(destination, physical, rows)

    def _select(self, select):
        source = select.args.get("from_")
        if source is None:
            return super()._select(select)
        table = source.this
        hosts = (self.coordinator,)
        if isinstance(table.this, exp.Anonymous):
            function = table.this
            cluster, database, relation, _trailing = cluster_arguments(function, "analytics")
            hosts = self.clusters[cluster]
            if function.name.casefold() == "cluster":
                hosts = (self._native_replica(cluster, self.coordinator),)
            else:
                assert function.name.casefold() == "clusterallreplicas"
            name = f"{database}.{relation}"
        else:
            name = _name(table)
        if not name.startswith("system."):
            rows = [row for host in hosts for row in self._read(host, name)]
            self.counts.append((name, hosts, len(rows)))
            if any(isinstance(item, exp.Count) for item in select.expressions):
                return [(len(rows),)]
            assert len(select.expressions) == 1
            assert isinstance(select.expressions[0], exp.Star)
            return deepcopy(rows)
        records = self._metadata(name, hosts)
        condition = select.args.get("where")
        records = [
            record
            for record in records
            if condition is None or _metadata_matches(condition.this, record)
        ]
        if any(isinstance(item, exp.Count) for item in select.expressions):
            return [(len(records),)]
        return [
            tuple(_metadata_project(item, record) for item in select.expressions)
            for record in records
        ]

    def _metadata(self, name, hosts):
        records = super()._metadata(name, hosts)
        if name == "system.clusters":
            for record in records:
                endpoints = self.clusters[record["cluster"]]
                names = [self.host_endpoints[host][0] for host in endpoints]
                record["shard_num"] = 1
                record["replica_num"] = names.index(record["host_name"]) + 1
                record["internal_replication"] = int(self.internal_replication)
        return records


class ReconfigureSubmissionLog:
    def __init__(self):
        self.sql = []

    @contextmanager
    def submission(self, statement):
        self.sql.append(statement)
        yield


class PhysicalHostSession:
    """A direct native session bound to one real fixture endpoint."""

    def __init__(self, raw, host):
        self.raw = raw
        self.host = host
        self.closed = False
        self.queries = []

    def query(self, statement, **_kwargs):
        self.queries.append(statement)
        select = parse_one(statement, read=ClickHouseDialect)
        assert isinstance(select, exp.Select)
        source = select.args.get("from_")
        if source is None:
            record = self.raw._host_record(self.host)
            rows = [tuple(_project(item, record) for item in select.expressions)]
        else:
            table = _name(source.this)
            physical = self.raw._read(self.host, table)
            assert any(isinstance(item, exp.Count) for item in select.expressions)
            rows = [(len(physical),)]
        return SimpleNamespace(result_rows=rows)

    def command(self, statement, **_kwargs):
        self.raw.worker_commands.append((self.host, statement))
        insert = parse_one(statement, read=ClickHouseDialect)
        assert isinstance(insert, exp.Insert)
        select = insert.expression
        assert isinstance(select, exp.Select)
        source = _name(select.args["from_"].this)
        self.raw.insert_rows(self.host, _name(insert.this), self.raw._read(self.host, source))

    def close(self):
        self.closed = True


def connect_independent_replicas(monkeypatch, raw):
    """Install the public coordinator and actual endpoint-local factory fakes."""
    connect_pair_scope(monkeypatch, raw)

    def connect(key, endpoint, *, port=None):
        assert key == "ch"
        host = next(
            name
            for name, (hostname, address, _native_port) in raw.host_endpoints.items()
            if str(endpoint) in {hostname, address}
        )
        assert (port if port is not None else getattr(endpoint, "tcp_port", None)) == (
            raw.host_endpoints[host][2]
        )
        session = PhysicalHostSession(raw, host)
        raw.worker_connections.append(session)
        return observed_connection(session)

    factory = import_module("analytics_toolkit.sql.connection.get_sql_connection")
    monkeypatch.setattr(factory, "get_ch_connection_for_host", connect)
