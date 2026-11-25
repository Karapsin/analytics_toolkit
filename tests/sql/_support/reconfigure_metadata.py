"""Projection-shaped server metadata for the legacy reconfiguration fixtures."""

from __future__ import annotations

from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.metadata import extract_clickhouse_distributed_shard_table
from analytics_toolkit.sql.backends.ch.outage_statements import exchange_or_rename
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import (
    engine_name,
    engine_sql,
    parse_create_table,
    retarget_create,
)
from sqlglot import exp, parse_one


def _identity(create):
    table = create.this.this
    return table.db or "analytics", table.name


def _endpoint(host):
    name, address, port = host
    return address or name, port


class ReconfigureMetadata:
    def __init__(self, client):
        self.client = client
        self.tables = {}
        self.distribution_queues = {}
        self.seeded = False

    @property
    def clusters(self):
        return {"core": self.client.source_hosts, "archive": self.client.target_hosts}

    @property
    def coordinator(self):
        hosts = self.client.source_hosts
        return hosts[0] if hosts else ("source", "10.0.0.1", 9000)

    def _hosts(self, cluster):
        if cluster is None:
            return (self.coordinator,)
        resolved = self.client.metadata_macros.get(cluster.strip("{}"), cluster)
        return self.clusters[resolved]

    @staticmethod
    def _creation_uuid(create):
        identity = create.meta.get("fixture_creation_uuid")
        if identity is None:
            declared = create.find(exp.UuidProperty)
            identity = UUID(declared.name) if declared is not None else uuid4()
            create.meta["fixture_creation_uuid"] = identity
        return identity

    def _created_definition(self, create, table, host=None):
        result = retarget_create(create, table, None)
        declared = create.find(exp.UuidProperty)
        result.meta["fixture_creation_uuid"] = UUID(declared.name) if declared else uuid4()
        if host is not None:
            self._replica_identity(result, _identity(result), host)
        return result

    def _macros(self, host):
        return {"shard": "1", "replica": host[0], **self.client.metadata_macros}

    def _replica_identity(self, create, identity, host):
        if not engine_name(create).casefold().startswith("replicated"):
            return None
        existing = create.meta.get("fixture_replication_identity")
        if existing is not None:
            return existing
        engine = create.find(exp.EngineProperty).this
        args = engine.expressions
        path, replica = (
            (args[0].this, args[1].this)
            if len(args) >= 2
            else (getattr(self.client, "metadata_replica_path", "/clickhouse/{table}"), "{replica}")
        )
        macros = {
            "database": identity[0],
            "table": identity[1],
            "uuid": str(self._creation_uuid(create)),
            **self._macros(host),
        }
        resolved = str(path).format_map(macros), str(replica).format_map(macros)
        create.meta["fixture_replication_identity"] = resolved
        return resolved

    def _seed(self):
        if self.seeded:
            return
        definitions = self.client.metadata_definitions()
        references = {
            (reference.database, reference.table)
            for ddl in definitions.values()
            if (
                reference := extract_clickhouse_distributed_shard_table(
                    engine_sql(parse_create_table(ddl, "metadata")), "analytics"
                )
            )
            is not None
        }
        for identity, ddl in definitions.items():
            create = parse_create_table(ddl, "metadata")
            hosts = (
                self.client.source_hosts
                if identity in references or engine_name(create) == "Distributed"
                else (self.coordinator,)
            )
            for host in hosts:
                self.tables[_endpoint(host), identity] = self._created_definition(
                    create, create.this.this.sql(dialect=ClickHouseDialect), host
                )
        self.seeded = True

    def rows(self, statement):
        tree = parse_one(statement, read=ClickHouseDialect)
        if not isinstance(tree, exp.Select):
            return None
        source = tree.args.get("from_")
        hosts = (self.coordinator,)
        if source is None:
            record = self._server(self.coordinator)
            return (
                None
                if any(isinstance(item, exp.Count) for item in tree.expressions)
                else [tuple(self._project(item, record) for item in tree.expressions)]
            )
        table = source.this
        if isinstance(table.this, exp.Anonymous):
            cluster, database, name, _trailing = cluster_arguments(table.this, "analytics")
            hosts = self._hosts(cluster)
            # This fixture models all configured members as replicas of shard1.
            if table.this.name.casefold() == "cluster":
                hosts = hosts[:1]
        else:
            database, name = table.db, table.name
        if database != "system" or name not in {
            "clusters",
            "one",
            "macros",
            "tables",
            "distribution_queue",
            "replicas",
            "server_settings",
        }:
            return None
        self._seed()
        records = self._records(name, hosts)
        condition = tree.args.get("where")
        records = [
            row for row in records if condition is None or self._matches(condition.this, row)
        ]
        if any(isinstance(item, exp.Count) for item in tree.expressions):
            return [(len(records),)]
        if all(isinstance(item, exp.Sum) for item in tree.expressions):
            return [
                tuple(
                    sum(self._project(item.this, row) for row in records)
                    for item in tree.expressions
                )
            ]
        return [tuple(self._project(item, row) for item in tree.expressions) for row in records]

    def show_create(self, statement):
        prefix = "SHOW CREATE TABLE "
        if not self.seeded or not statement.startswith(prefix):
            return None
        table = parse_one(statement[len(prefix) :], read=ClickHouseDialect, into=exp.Table)
        identity = table.db or "analytics", table.name
        create = self.tables.get((_endpoint(self.coordinator), identity))
        if create is None:
            message = f"Fixture table {table.sql(dialect=ClickHouseDialect)} does not exist."
            raise RuntimeError(message)
        return create.sql(dialect=ClickHouseDialect)

    @staticmethod
    def _server(host):
        return {
            "serverUUID": str(uuid5(NAMESPACE_DNS, repr(_endpoint(host)))),
            "tcpPort": host[2],
            "hostName": host[0],
            "currentDatabase": "analytics",
        }

    def _replication_records(self, name, origins):
        if name == "replicas":
            return [
                {
                    "_shard_num": position,
                    "database": identity[0],
                    "table": identity[1],
                    "zookeeper_path": replica[0],
                    "replica_name": replica[1],
                    "zookeeper_name": "",
                    "is_readonly": 0,
                    "is_session_expired": 0,
                }
                for position, host in enumerate(origins, 1)
                for (endpoint, identity), create in self.tables.items()
                if endpoint == _endpoint(host)
                and (replica := self._replica_identity(create, identity, host)) is not None
            ]
        if name == "server_settings":
            return [
                {"_shard_num": position, "name": setting, "value": value}
                for position, _host in enumerate(origins, 1)
                for setting, value in (
                    (
                        "default_replica_path",
                        getattr(self.client, "metadata_replica_path", "/clickhouse/{table}"),
                    ),
                    ("default_replica_name", "{replica}"),
                )
            ]

        return None

    def _records(self, name, origins):
        if name in {"replicas", "server_settings"}:
            return self._replication_records(name, origins)
        if name == "distribution_queue":
            return [
                {
                    "_shard_num": position,
                    "database": database,
                    "table": table,
                    "data_files": counters[0],
                    "broken_data_files": counters[1],
                }
                for position, host in enumerate(origins, 1)
                for (endpoint, database, table), counters in self.distribution_queues.items()
                if endpoint == _endpoint(host)
            ]
        if name == "clusters":
            return [
                {
                    "_shard_num": position,
                    "cluster": cluster,
                    "host_name": host[0],
                    "host_address": host[1],
                    "port": host[2],
                    "shard_num": 1,
                    "replica_num": replica,
                    "is_local": int(_endpoint(host) == _endpoint(origin)),
                }
                for position, origin in enumerate(origins, 1)
                for cluster, hosts in self.clusters.items()
                for replica, host in enumerate(hosts, 1)
            ]
        if name == "macros":
            return [
                {"_shard_num": position, "macro": macro, "substitution": value}
                for position, origin in enumerate(origins, 1)
                for macro, value in self._macros(origin).items()
            ]
        if name == "one":
            return [
                {"_shard_num": position, **self._server(host)}
                for position, host in enumerate(origins, 1)
            ]
        return [
            {
                "_shard_num": position,
                "database": identity[0],
                "name": identity[1],
                "engine": engine_name(create),
                "engine_full": engine_sql(create),
                "create_table_query": create.sql(dialect=ClickHouseDialect),
                "uuid": str(self._creation_uuid(create)),
            }
            for position, host in enumerate(origins, 1)
            for (endpoint, identity), create in self.tables.items()
            if endpoint == _endpoint(host)
        ]

    def _project(self, item, row):
        if isinstance(item, exp.Alias):
            return self._project(item.this, row)
        if isinstance(item, exp.Literal):
            return item.to_py()
        if isinstance(item, exp.Cast):
            return str(self._project(item.this, row))
        if isinstance(item, exp.Anonymous) and item.name.casefold() == "tostring":
            return str(self._project(item.expressions[0], row))
        if isinstance(item, exp.Anonymous) and item.name.casefold() == "getmacro":
            return self.client.metadata_macros[item.expressions[0].name]
        name = item.name or (item.sql_name().replace("_", "") if isinstance(item, exp.Func) else "")
        return next(value for key, value in row.items() if key.casefold() == name.casefold())

    def _matches(self, item, row):
        if isinstance(item, exp.And):
            return self._matches(item.this, row) and self._matches(item.expression, row)
        if isinstance(item, exp.Or):
            return self._matches(item.this, row) or self._matches(item.expression, row)
        if isinstance(item, exp.In):
            return self._project(item.this, row) in {
                self._project(value, row) for value in item.expressions
            }
        assert isinstance(item, exp.EQ)
        return self._project(item.this, row) == self._project(item.expression, row)

    def command(self, statement):
        self._seed()
        special = exchange_or_rename(statement)
        if special is not None:
            operation, scopes = special
            tables = operation.expressions
            hosts = self._hosts(scopes[0].strip("'\"`")) if scopes else (self.coordinator,)
            for index in range(0, len(tables), 2):
                source, target = tables[index : index + 2]
                self._rename(source, target, hosts, exchange=operation.this == "EXCHANGE")
            return
        tree = parse_one(statement, read=ClickHouseDialect)
        if isinstance(tree, exp.Create):
            scope = tree.find(exp.OnCluster)
            name = scope.this.name if scope is not None else None
            hosts = self._hosts(name)
            for host in hosts:
                key = _endpoint(host), _identity(tree)
                if key in self.tables and tree.args.get("exists"):
                    continue
                self.tables[key] = self._created_definition(
                    tree, tree.this.this.sql(dialect=ClickHouseDialect), host
                )
        elif isinstance(tree, exp.Drop):
            scope = tree.find(exp.OnCluster)
            name = scope.this.name if scope is not None else None
            tables = tree.args.get("tables") or [tree.this]
            for table in tables:
                assert isinstance(table, exp.Table)
                identity = table.db or "analytics", table.name
                for host in self._hosts(name):
                    self.tables.pop((_endpoint(host), identity), None)

    def _rename(self, source, target, hosts, *, exchange):
        source_id = source.db or "analytics", source.name
        target_id = target.db or "analytics", target.name
        for host in hosts:
            source_key, target_key = (_endpoint(host), source_id), (_endpoint(host), target_id)
            assert source_key in self.tables
            if exchange:
                assert target_key in self.tables
                self.tables[source_key], self.tables[target_key] = (
                    self.tables[target_key],
                    self.tables[source_key],
                )
                self.tables[source_key] = retarget_create(
                    self.tables[source_key], source.sql(dialect=ClickHouseDialect), None
                )
            else:
                assert target_key not in self.tables
                self.tables[target_key] = self.tables.pop(source_key)
            self.tables[target_key] = retarget_create(
                self.tables[target_key], target.sql(dialect=ClickHouseDialect), None
            )
