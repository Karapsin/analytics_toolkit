"""Two-host state behind the real outage wrapper for managed-pair conversion."""

from __future__ import annotations

from copy import deepcopy
from importlib import import_module

from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.backends.ch.outage_topology import EXECUTION_SETTINGS, PROBE_SETTINGS
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import engine_sql, parse_create_table
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.execution.observation import observed_connection
from sqlglot import exp

from tests.sql._support.reconfigure import reconfigure_api
from tests.sql._support.reconfigure_conversion_scope import (
    ConversionScopeClient,
    _name,
    _project,
)


def configure_pair_scope(write_sql_connections, facade_scope):
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
                "ddl_defaults": {
                    "regular": {
                        "create_distributed_pair": False,
                        "shard": {"engine": "MergeTree", "on_cluster": "storage"},
                        "distributed": {
                            "engine_template": (
                                "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                            ),
                            "cluster": "storage",
                            "on_cluster": facade_scope,
                            "sharding_key": "rand()",
                        },
                    }
                },
            }
        }
    )
    set_connections_path(path)


def connect_pair_scope(monkeypatch, raw):
    import_module("tests.sql._support.reconfigure_host_view").install_conversion_host_views(
        monkeypatch, raw
    )

    def connect(key):
        wrapped = OutageClient(observed_connection(raw), get_connection_config(key))
        raw.wrappers.append(wrapped)
        return wrapped

    monkeypatch.setattr(reconfigure_api, "get_sql_connection", connect)


def _metadata_matches(expression, record):
    if isinstance(expression, exp.Paren):
        return _metadata_matches(expression.this, record)
    if isinstance(expression, exp.And):
        return _metadata_matches(expression.this, record) and _metadata_matches(
            expression.expression, record
        )
    if isinstance(expression, exp.Or):
        return _metadata_matches(expression.this, record) or _metadata_matches(
            expression.expression, record
        )
    if isinstance(expression, exp.In):
        return _metadata_project(expression.this, record) in {
            item.to_py() for item in expression.expressions
        }
    assert isinstance(expression, exp.EQ)
    return _metadata_project(expression.this, record) == _metadata_project(
        expression.expression, record
    )


def _metadata_project(expression, record):
    if isinstance(expression, exp.Cast):
        return str(_metadata_project(expression.this, record))
    if isinstance(expression, exp.Anonymous) and expression.name.casefold() == "tostring":
        assert len(expression.expressions) == 1
        return str(_metadata_project(expression.expressions[0], record))
    return _project(expression, record)


class PairScopeClient(ConversionScopeClient):
    def __init__(self, *, managed_remote_facade=False):
        self.wrappers = []
        self.counts = []
        super().__init__()
        self.clusters["storage"] = (self.remote,)
        self.clusters["frontends"] = (self.remote,)
        self.host_tables[self.coordinator].pop(self.source_table)
        shard = parse_create_table(
            "CREATE TABLE analytics.events_shard (id UInt64) ENGINE=MergeTree ORDER BY id",
            "analytics.events_shard",
        )
        self._create(self.remote, shard, [(1,), (2,), (3,)])
        facade = parse_create_table(
            "CREATE TABLE analytics.events (id UInt64) "
            "ENGINE=Distributed('storage', 'analytics', 'events_shard', rand())",
            self.source_table,
        )
        self._create(self.coordinator, facade)
        if managed_remote_facade:
            self.host_tables[self.remote].pop(self.source_table)
            self._create(self.remote, facade)
        self.created.clear()

    def snapshot(self):
        return {
            host: {
                name: {
                    "uuid": value.uuid,
                    "ddl": value.create.sql(dialect=ClickHouseDialect),
                    "rows": deepcopy(value.rows),
                    "route": value.route,
                }
                for name, value in tables.items()
            }
            for host, tables in self.host_tables.items()
        }

    def _select(self, select):
        source = select.args.get("from_")
        if source is None:
            return super()._select(select)
        table = source.this
        hosts = (self.coordinator,)
        if isinstance(table.this, exp.Anonymous):
            cluster, database, relation, _trailing = cluster_arguments(table.this, "analytics")
            hosts = self.clusters[cluster]
            name = f"{database}.{relation}"
        else:
            name = _name(table)
        if not name.startswith("system."):
            count = sum(len(self._read(host, name)) for host in hosts)
            self.counts.append((name, hosts, count))
            return [(count,)]
        records = self._metadata(name, hosts)
        condition = select.args.get("where")
        records = [
            record
            for record in records
            if condition is None or _metadata_matches(condition.this, record)
        ]
        if any(isinstance(item, exp.Count) for item in select.expressions):
            return [(len(records),)]
        if all(isinstance(item, exp.Sum) for item in select.expressions):
            return [
                tuple(
                    sum(_metadata_project(item.this, record) for record in records)
                    for item in select.expressions
                )
            ]
        return [
            tuple(_metadata_project(item, record) for item in select.expressions)
            for record in records
        ]

    def _metadata(self, name, hosts):
        if name == "system.settings":
            settings = {
                **PROBE_SETTINGS,
                **dict.fromkeys(EXECUTION_SETTINGS, 0),
                "distributed_ddl_output_mode": "throw",
                "skip_unavailable_shards_mode": "unavailable_or_table_missing",
            }
            return [
                {"name": setting, "value": str(value), "readonly": 0}
                for setting, value in settings.items()
            ]
        if name == "system.clusters":
            return [
                {
                    "_shard_num": position,
                    "cluster": cluster,
                    "host_name": self.host_endpoints[endpoint][0],
                    "host_address": self.host_endpoints[endpoint][1],
                    "port": self.host_endpoints[endpoint][2],
                    "shard_num": index + 1,
                    "replica_num": 1,
                    "is_local": int(endpoint == origin),
                }
                for position, origin in enumerate(hosts, 1)
                for cluster, endpoints in self.clusters.items()
                for index, endpoint in enumerate(endpoints)
            ]
        records = super()._metadata(name, hosts)
        if name not in {"system.tables", "system.columns"}:
            return records
        for record in records:
            host = next(
                host for host in hosts if self.server_identities[host][0] == record["serverUUID"]
            )
            relation = record["table"] if name == "system.columns" else record["name"]
            value = self.host_tables[host][f"{record['database']}.{relation}"]
            record.update(engine_full=engine_sql(value.create), uuid=value.uuid)
        return records
