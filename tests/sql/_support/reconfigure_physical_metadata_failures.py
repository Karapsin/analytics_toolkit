"""External metadata faults after a truthful physical endpoint proof."""

from __future__ import annotations

import json
from copy import deepcopy
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.execution.observation import observed_connection
from sqlglot import exp, parse_one

from tests.sql._support.reconfigure_independent_replicas import (
    IndependentReplicaClient,
    PhysicalHostSession,
    configure_independent_replicas,
    connect_independent_replicas,
)


def _system_relation(select):
    source = select.args.get("from_")
    if source is None:
        return None
    table = source.this
    if isinstance(table.this, exp.Anonymous):
        _cluster, database, relation, _trailing = cluster_arguments(table.this, "analytics")
        return database, relation
    return table.db, table.name


class PhysicalMetadataClient(IndependentReplicaClient):
    def __init__(self, *, member_rows=None, change_ddl=False, shared_hostname=False):
        super().__init__(internal_replication=True)
        self.member_rows = deepcopy(member_rows)
        self.change_ddl = change_ddl
        self.membership_faults = []
        self.complete_memberships = []
        self.complete_identity_probes = []
        self.weak_source_definitions = []
        self.changed_snapshot = None
        self.http_worker_opens = []
        if shared_hostname:
            self.host_endpoints[self.coordinator] = ("shared-host", "10.0.0.1", 9000)
            self.host_endpoints[self.remote] = ("shared-host", "10.0.0.2", 9001)
            identity, _port = self.server_identities[self.remote]
            self.server_identities[self.remote] = identity, 9001

    def _metadata(self, name, hosts):
        records = super()._metadata(name, hosts)
        if name == "system.clusters":
            replicas = {
                (self.host_endpoints[host][1], self.host_endpoints[host][2]): position
                for position, host in enumerate(self.clusters["storage"], 1)
            }
            for record in records:
                if record["cluster"] == "storage":
                    endpoint = record["host_address"], record["port"]
                    record.update(shard_num=1, replica_num=replicas[endpoint])
        return records

    def query(self, statement, settings=None):
        result = super().query(statement, settings=settings)
        if statement.startswith(("SHOW ", "EXISTS ")):
            return result
        select = parse_one(statement, read=ClickHouseDialect)
        assert isinstance(select, exp.Select)
        relation = _system_relation(select)
        columns = tuple(item.name for item in select.expressions)
        if relation == ("system", "clusters"):
            if len(select.expressions) == 6:
                self.complete_memberships.append(deepcopy(result.result_rows))
            if (
                columns == ("shard_num", "replica_num", "host_name")
                and self.member_rows is not None
            ):
                self.membership_faults.append(deepcopy(result.result_rows))
                return SimpleNamespace(result_rows=deepcopy(self.member_rows))
        elif relation == ("system", "one") and len(select.expressions) == 3:
            self.complete_identity_probes.append(deepcopy(result.result_rows))
        elif relation == ("system", "tables") and columns == ("create_table_query",):
            if self.change_ddl and not self.weak_source_definitions:
                self.weak_source_definitions = deepcopy(result.result_rows)
                self.host_tables[self.remote][self.source_physical].create = parse_create_table(
                    "CREATE TABLE analytics.events_shard (id UInt64) "
                    "ENGINE=MergeTree ORDER BY cityHash64(id)",
                    self.source_physical,
                )
                self.changed_snapshot = self.snapshot()
        return result


def install_metadata_fault(monkeypatch, write_sql_connections, **options):
    configure_independent_replicas(write_sql_connections)
    raw = PhysicalMetadataClient(**options)
    connect_independent_replicas(monkeypatch, raw)
    return raw


def install_http_metadata(monkeypatch, write_sql_connections, *, shared_hostname=False):
    raw = install_metadata_fault(
        monkeypatch, write_sql_connections, shared_hostname=shared_hostname
    )
    configs = json.loads(Path(".connections").read_text())
    configs["ch"].update(
        driver="http", port=8123, host="shared-host" if shared_hostname else "coordinator"
    )
    set_connections_path(write_sql_connections(configs))

    def connect_http(key, endpoint, *, port=None):
        config = get_connection_config(key)
        assert config.driver == "http"
        http_port = config.port if port is None else port
        assert http_port == 8123
        candidates = [
            host
            for host, (hostname, address, _native_port) in raw.host_endpoints.items()
            if str(endpoint) in {hostname, address}
        ]
        assert len(candidates) == 1, "HTTP host selection cannot use a native TCP port hint."
        host = candidates[0]
        raw.http_worker_opens.append((host, http_port, getattr(endpoint, "tcp_port", None)))
        session = PhysicalHostSession(raw, host)
        raw.worker_connections.append(session)
        return observed_connection(session)

    factory = import_module("analytics_toolkit.sql.connection.get_sql_connection")
    monkeypatch.setattr(factory, "get_ch_connection_for_host", connect_http)
    return raw
