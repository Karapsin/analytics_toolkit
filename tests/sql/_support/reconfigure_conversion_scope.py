"""Two-host SQL state for local-to-pair reconfiguration ownership."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar
from uuid import uuid4

from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.metadata import extract_clickhouse_distributed_shard_table
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import (
    engine_name,
    engine_sql,
    parse_create_table,
    retarget_create,
)
from sqlglot import exp, parse_one

if TYPE_CHECKING:
    from sqlglot.tokens import TokenType


def configure_conversion(write_sql_connections):
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
                        "create_distributed_pair": True,
                        "shard": {"engine": "MergeTree", "on_cluster": "core"},
                        "distributed": {
                            "engine_template": (
                                "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                            ),
                            "cluster": "core",
                            "on_cluster": None,
                            "sharding_key": "rand()",
                        },
                    }
                },
            }
        }
    )
    set_connections_path(path)


def convert(facade_scope, **kwargs):
    return sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        ch_distributed_table=True,
        ch_shard_on_cluster="core",
        ch_distributed_cluster="core",
        ch_distributed_on_cluster=facade_scope,
        retry_cnt=1,
        timeout_increment=0,
        **kwargs,
    )


class _DDLTokenizer(ClickHouseDialect.tokenizer_class):
    # The default tokenizer collapses RENAME's whole tail into one command token.
    # These SQL-state commands need ordinary identifier/TO/ON CLUSTER tokens.
    COMMANDS: ClassVar[set[TokenType]] = set()


class _ConversionTable(SimpleNamespace):
    def __init__(self, **attributes):
        super().__init__(_resource_uuid=uuid4(), **attributes)

    @property
    def uuid(self):
        return self._resource_uuid


def _name(table):
    if isinstance(table, str):
        table = parse_one(f"SELECT * FROM {table}", read=ClickHouseDialect).find(exp.Table)
    assert isinstance(table, exp.Table)
    return f"{table.db or 'analytics'}.{table.name}"


def _scope_sql(statement):
    tokens = _DDLTokenizer().tokenize(statement)
    for index, token in enumerate(tokens):
        words = token.text.upper().split()
        width = 1 if words == ["ON", "CLUSTER"] else 2
        if words != ["ON", "CLUSTER"] and not (
            words == ["ON"] and tokens[index + 1].text.upper() == "CLUSTER"
        ):
            continue
        cluster_token = tokens[index + width]
        return (
            cluster_token.text,
            statement[: token.start] + statement[cluster_token.end + 1 :],
        )
    return None, statement


def _rename_pairs(statement, *, exchange=False):
    tokens = _DDLTokenizer().tokenize(statement)
    separator = "AND" if exchange else "TO"
    pairs = []
    start = 2
    source = None
    for index in range(start, len(tokens) + 1):
        word = tokens[index].text.upper() if index < len(tokens) else ","
        if word not in {separator, ","}:
            continue
        fragment = statement[tokens[start].start : tokens[index - 1].end + 1]
        if word == separator:
            source = _name(fragment)
        else:
            assert source is not None
            pairs.append((source, _name(fragment)))
            source = None
        start = index + 1
    assert pairs
    return pairs


class ConversionScopeClient:
    coordinator = "coordinator"
    remote = "remote"
    source_table = "analytics.events"

    def __init__(self):
        self.clusters = {
            "core": (self.coordinator, self.remote),
            "coordinator_only": (self.coordinator,),
        }
        self.host_tables = {self.coordinator: {}, self.remote: {}}
        self.host_endpoints = {
            self.coordinator: (self.coordinator, "10.0.0.1", 9000),
            self.remote: (self.remote, "10.0.0.2", 9000),
        }
        self.server_identities = {
            self.coordinator: ("00000000-0000-0000-0000-000000000001", 9000),
            self.remote: ("00000000-0000-0000-0000-000000000002", 9000),
        }
        self.local_candidates = {self.coordinator}
        self.commands = []
        self.command_settings = []
        self.host_commands = {host: [] for host in self.host_tables}
        self.queries = []
        self.query_settings = []
        self.created = []
        self.ready_queries = []
        self.closed = False
        self.after_facade_cutover = None
        self._facade_callback_called = False
        source = parse_create_table(
            "CREATE TABLE analytics.events (id UInt64) ENGINE=MergeTree ORDER BY id",
            self.source_table,
        )
        self._create(self.coordinator, source, [(1,), (2,), (3,)])
        self.remote_original = [(value,) for value in range(1001, 1101)]
        self._create(self.remote, source, self.remote_original)
        self.created.clear()

    @property
    def host_rows(self):
        return {
            host: {table: value.rows for table, value in tables.items()}
            for host, tables in self.host_tables.items()
        }

    def rows(self, host, table):
        name = _name(table)
        if name not in self.host_tables[host]:
            return []
        return deepcopy(self._read(host, name))

    def _create(self, host, create, rows=()):
        name = _name(create.this.this)
        assert name not in self.host_tables[host]
        reference = extract_clickhouse_distributed_shard_table(engine_sql(create), "analytics")
        self.host_tables[host][name] = _ConversionTable(
            create=retarget_create(create, name, None),
            engine=engine_name(create),
            schema={
                column.name: column.kind.sql(dialect=ClickHouseDialect)
                for column in create.this.expressions
                if isinstance(column, exp.ColumnDef)
            },
            route=(reference.cluster, f"{reference.database}.{reference.table}")
            if reference is not None
            else None,
            rows=deepcopy(list(rows)),
        )
        self.created.append((host, name))

    def _read(self, host, table):
        value = self.host_tables[host][table]
        if value.route is None:
            return value.rows
        cluster, physical = value.route
        return [
            row for endpoint in self.clusters[cluster] for row in self._read(endpoint, physical)
        ]

    def insert_rows(self, host, table, rows):
        value = self.host_tables[host][_name(table)]
        if value.route is None:
            value.rows.extend(deepcopy(list(rows)))
            return
        cluster, physical = value.route
        hosts = self.clusters[cluster]
        for row in rows:
            self.insert_rows(hosts[int(row[0]) % len(hosts)], physical, [row])

    def command(self, statement, settings=None):
        self.commands.append(statement)
        self.command_settings.append(settings)
        cluster, local = _scope_sql(statement)
        hosts = self.clusters[cluster] if cluster is not None else (self.coordinator,)
        for host in hosts:
            self.host_commands[host].append(statement)
        operation = local.lstrip().split(None, 1)[0].upper()
        if operation in {"RENAME", "EXCHANGE"}:
            self._rename(local, hosts, exchange=operation == "EXCHANGE")
            return
        tree = parse_one(local, read=ClickHouseDialect)
        if isinstance(tree, exp.Create):
            for host in hosts:
                self._create(host, tree)
        elif isinstance(tree, exp.Drop):
            tables = tree.args.get("tables") or [tree.this]
            for table in tables:
                name = _name(table)
                for host in hosts:
                    assert tree.args.get("exists") or name in self.host_tables[host]
                    self.host_tables[host].pop(name, None)
        elif isinstance(tree, exp.Insert):
            assert hosts == (self.coordinator,)
            source = _name(tree.expression.args["from_"].this)
            incoming = deepcopy(self._read(self.coordinator, source))
            self.insert_rows(self.coordinator, _name(tree.this), incoming)
        else:
            message = f"Unsupported SQL command at the conversion endpoint: {statement}"
            raise TypeError(message)

    def _rename(self, statement, hosts, *, exchange):
        for source, target in _rename_pairs(statement, exchange=exchange):
            for host in hosts:
                tables = self.host_tables[host]
                assert source in tables
                if exchange:
                    assert target in tables
                    tables[source], tables[target] = tables[target], tables[source]
                    tables[source].create = retarget_create(tables[source].create, source, None)
                else:
                    assert target not in tables
                    tables[target] = tables.pop(source)
                tables[target].create = retarget_create(tables[target].create, target, None)
            facade = self.host_tables[self.coordinator].get(target)
            if (
                target == self.source_table
                and facade is not None
                and facade.route is not None
                and not self._facade_callback_called
            ):
                self._facade_callback_called = True
                if self.after_facade_cutover is not None:
                    self.after_facade_cutover()

    def query(self, statement, settings=None):
        self.queries.append(statement)
        self.query_settings.append(deepcopy(settings))
        if statement.startswith("SHOW CREATE TABLE "):
            name = _name(statement[len("SHOW CREATE TABLE ") :])
            value = self.host_tables[self.coordinator][name]
            rows = [(value.create.sql(dialect=ClickHouseDialect),)]
        elif statement.startswith("EXISTS TABLE "):
            self.ready_queries.append(statement)
            name = _name(statement[len("EXISTS TABLE ") :])
            rows = [(int(name in self.host_tables[self.coordinator]),)]
        else:
            rows = self._select(parse_one(statement, read=ClickHouseDialect))
        return SimpleNamespace(result_rows=rows)

    def _select(self, select):
        source = select.args.get("from_")
        if source is None:
            record = {
                **self._host_record(self.coordinator),
                "getMacro": "core",
                "currentDatabase": "analytics",
            }
            return [tuple(_project(item, record) for item in select.expressions)]
        table = source.this
        hosts = (self.coordinator,)
        if isinstance(table.this, exp.Anonymous):
            function = table.this
            assert function.name.lower() in {"cluster", "clusterallreplicas"}
            cluster, database, relation = (argument.name for argument in function.expressions)
            hosts = self.clusters[cluster]
            name = f"{database}.{relation}"
        else:
            name = _name(table)
        if not name.startswith("system."):
            return [(sum(len(self._read(host, name)) for host in hosts),)]
        records = self._metadata(name, hosts)
        condition = select.args.get("where")
        records = [
            record for record in records if condition is None or _matches(condition.this, record)
        ]
        if any(isinstance(item, exp.Count) for item in select.expressions):
            return [(len(records),)]
        return [tuple(_project(item, record) for item in select.expressions) for record in records]

    def _host_record(self, host):
        identity, plain_port = self.server_identities[host]
        return {
            "hostName": self.host_endpoints[host][0],
            "serverUUID": identity,
            "tcpPort": plain_port,
        }

    def _metadata(self, name, hosts):
        if name == "system.databases":
            return [{"name": "analytics", "engine": "Atomic"}]
        if name == "system.clusters":
            return [
                {
                    "cluster": cluster,
                    "host_name": self.host_endpoints[host][0],
                    "host_address": self.host_endpoints[host][1],
                    "port": self.host_endpoints[host][2],
                    "shard_num": index + 1,
                    "replica_num": 1,
                    "is_local": int(host in self.local_candidates),
                }
                for cluster, endpoints in self.clusters.items()
                for index, host in enumerate(endpoints)
            ]
        if name == "system.one":
            return [
                {**self._host_record(host), "_shard_num": index}
                for index, host in enumerate(hosts, 1)
            ]
        records = []
        for index, host in enumerate(hosts, 1):
            for table, value in self.host_tables[host].items():
                database, relation = table.split(".", 1)
                record = {
                    **self._host_record(host),
                    "_shard_num": index,
                    "database": database,
                    "name": relation,
                    "engine": value.engine,
                    "uuid": value.uuid,
                    "create_table_query": value.create.sql(dialect=ClickHouseDialect),
                }
                if name == "system.columns":
                    records.extend(
                        {**record, "table": relation, "name": column, "type": native}
                        for column, native in value.schema.items()
                    )
                else:
                    assert name == "system.tables"
                    records.append(record)
        return records

    def close(self):
        self.closed = True


def _project(expression, record):
    if isinstance(expression, exp.Alias):
        expression = expression.this
    if isinstance(expression, exp.Literal):
        return expression.to_py()
    name = expression.name
    if not name and isinstance(expression, exp.Func):
        name = expression.sql_name().replace("_", "")
    return next(value for key, value in record.items() if key.casefold() == name.casefold())


def _matches(expression, record):
    if isinstance(expression, exp.Paren):
        return _matches(expression.this, record)
    if isinstance(expression, exp.And):
        return _matches(expression.this, record) and _matches(expression.expression, record)
    if isinstance(expression, exp.Or):
        return _matches(expression.this, record) or _matches(expression.expression, record)
    assert isinstance(expression, exp.EQ)
    return record[expression.this.name] == expression.expression.to_py()
