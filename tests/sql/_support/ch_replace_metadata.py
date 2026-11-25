"""Projected native instance and resource evidence for replacement fixtures."""

from __future__ import annotations

from ipaddress import IPv6Address
from uuid import NAMESPACE_URL, UUID, uuid5

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import retarget_create
from sqlglot import exp, parse_one


class ReplacementMetadata:
    def __init__(self, client):
        self.client = client

    def _scope(self, scope):
        visited = set()
        while scope in self.client.scope_aliases:
            assert scope not in visited, scope
            visited.add(scope)
            scope = self.client.scope_aliases[scope]
        return scope

    def _instances(self, scope):
        scope = self._scope(scope)
        count = (
            1 if scope == self._scope(self.client.coordinator_scope) else self.client.replica_count
        )
        return [(scope, replica) for replica in range(1, count + 1)]

    @staticmethod
    def _instance_record(instance):
        scope, replica = instance
        name = f"{scope}-replica-{replica}"
        identity = uuid5(NAMESPACE_URL, f"https://fixture.invalid/ch-replace/{name}")
        address = IPv6Address((0x20010DB8 << 96) | (identity.int & ((1 << 96) - 1)))
        return {
            "host_name": name,
            "host_address": str(address),
            "hostName": name,
            "port": 9000,
            "serverUUID": str(identity),
            "tcpPort": 9000,
        }

    def local_identity(self):
        record = self._instance_record(self._instances(self.client.coordinator_scope)[0])
        return record["serverUUID"], record["tcpPort"]

    def ddl(self, key):
        scope, name = self._scope(key[0]), key[1]
        key = scope, name
        value = self.client.tables[key]
        if value.create is not None:
            return retarget_create(value.create, name, None).sql(dialect=ClickHouseDialect)
        table = _table(name)
        types = self.client.column_types.get(key, ("Int64",) * len(value.columns))
        columns = ", ".join(
            exp.ColumnDef(
                this=exp.to_identifier(column, quoted=True),
                kind=exp.DataType.build(native, dialect=ClickHouseDialect),
            ).sql(dialect=ClickHouseDialect)
            for column, native in zip(value.columns, types)
        )
        source = self.client.facade_sources.get(key)
        if source is None:
            engine = exp.Anonymous(this="ReplicatedMergeTree" if value.replicated else "MergeTree")
            order = " ORDER BY tuple()"
        else:
            cluster, physical = source
            physical_table = _table(physical)
            engine = exp.Anonymous(
                this="Distributed",
                expressions=[
                    exp.Literal.string(cluster),
                    exp.Literal.string(physical_table.db or self.client.current_database),
                    exp.Literal.string(physical_table.name),
                    exp.Anonymous(this="rand"),
                ],
            )
            order = ""
        return (
            f"CREATE TABLE {table.sql(dialect=ClickHouseDialect)} ({columns}) "
            f"ENGINE = {engine.sql(dialect=ClickHouseDialect)}{order}"
        )

    def _cluster_names(self, tree):
        condition = tree.args.get("where")
        if condition is None:
            return set()
        names = set()
        for comparison in condition.find_all(exp.EQ):
            if isinstance(comparison.this, exp.Column) and comparison.this.name == "cluster":
                names.add(str(_project(comparison.expression, {})))
        for comparison in condition.find_all(exp.In):
            if isinstance(comparison.this, exp.Column) and comparison.this.name == "cluster":
                names.update(str(_project(item, {})) for item in comparison.expressions)
        return names

    def _cluster_records(self, tree, scope):
        names = self._cluster_names(tree)
        records = []
        for position, origin in enumerate(self._instances(scope), 1):
            for cluster in sorted(names):
                for _target_scope, replica in self._instances(cluster):
                    instance = self._scope(cluster), replica
                    records.append(
                        {
                            **self._instance_record(instance),
                            "_shard_num": position,
                            "cluster": cluster,
                            "shard_num": 1,
                            "replica_num": replica,
                            "is_local": int(instance == origin),
                            "internal_replication": int(self.client.internal_replication),
                        }
                    )
        return records

    def _table_records(self, scope):
        scope = self._scope(scope)
        records = []
        for position, instance in enumerate(self._instances(scope), 1):
            for (resource_scope, name), value in self.client.tables.items():
                if self._scope(resource_scope) != scope:
                    continue
                table = _table(name)
                ddl = self.ddl((scope, name))
                create = parse_one(ddl, read=ClickHouseDialect)
                engine = create.find(exp.EngineProperty).this
                records.append(
                    {
                        **self._instance_record(instance),
                        "_shard_num": position,
                        "database": table.db or self.client.current_database,
                        "name": table.name,
                        "engine": "Distributed"
                        if (scope, name) in self.client.facade_sources
                        else "ReplicatedMergeTree"
                        if value.replicated
                        else "MergeTree",
                        "engine_full": engine.sql(dialect=ClickHouseDialect),
                        "create_table_query": ddl,
                        "uuid": self._table_uuid(scope, table, value),
                    }
                )
        return records

    def _table_uuid(self, scope, table, value):
        database = table.db or self.client.current_database
        engine = self.client.database_engines.get((scope, database), self.client.engines[scope])
        return UUID(int=0) if engine == "Ordinary" else value.uuid

    def _database_records(self, scope):
        databases = {
            _table(name).db or self.client.current_database
            for _resource_scope, name in self.client.tables
        }
        return [
            {
                "_shard_num": position,
                "name": database,
                "engine": self.client.database_engines.get(
                    (self._scope(scope), database), self.client.engines[self._scope(scope)]
                ),
            }
            for position, _instance in enumerate(self._instances(scope), 1)
            for database in databases
        ]

    def _replica_records(self, scope):
        records = []
        for record in self._table_records(scope):
            if not record["engine"].casefold().startswith("replicated"):
                continue
            create = parse_one(record["create_table_query"], read=ClickHouseDialect)
            arguments = create.find(exp.EngineProperty).this.expressions
            template = (
                arguments[0].this if len(arguments) >= 2 else self.client.default_replica_path
            )
            values = {
                "database": record["database"],
                "table": record["name"],
                "uuid": str(record["uuid"]),
                "shard": "1",
                "replica": record["host_name"],
            }
            records.append(
                {
                    **record,
                    "table": record["name"],
                    "zookeeper_path": template.format_map(values),
                }
            )
        return records

    def _column_records(self, scope):
        return [
            {
                **record,
                "table": record["name"],
                "name": column.name,
                "type": column.kind.sql(dialect=ClickHouseDialect),
                "default_kind": "",
                "default_type": "",
                "default_expression": "",
            }
            for record in self._table_records(scope)
            for column in parse_one(record["create_table_query"], read=ClickHouseDialect).find_all(
                exp.ColumnDef
            )
        ]

    def _macro_records(self, scope):
        records = [
            {
                "_shard_num": position,
                "macro": macro[1:-1],
                "substitution": self._scope(value),
            }
            for position, _instance in enumerate(self._instances(scope), 1)
            for macro, value in self.client.scope_aliases.items()
            if macro.startswith("{") and macro.endswith("}")
        ]
        records.extend(
            {"_shard_num": position, "macro": macro, "substitution": value}
            for position, instance in enumerate(self._instances(scope), 1)
            for macro, value in (
                ("shard", "1"),
                ("replica", self._instance_record(instance)["host_name"]),
            )
        )
        return records

    def _native_records(self, tree, scope, table, fields):
        if table == "system.clusters" and ("host_address" in fields or "is_local" in fields):
            return self._cluster_records(tree, scope)
        if table == "system.one" and "serveruuid" in fields:
            return [
                {**self._instance_record(instance), "_shard_num": position}
                for position, instance in enumerate(self._instances(scope), 1)
            ]
        return None

    def _server_setting_records(self, scope):
        return [
            {
                "_shard_num": position,
                "name": "default_replica_path",
                "value": self.client.default_replica_path,
            }
            for position, _instance in enumerate(self._instances(scope), 1)
        ]

    def _catalog_records(self, tree, scope, table):
        fields = tuple(item.name.casefold() for item in tree.expressions)
        if table in {"system.clusters", "system.one"}:
            return self._native_records(tree, scope, table, fields)
        if table == "system.tables" and any(
            name in fields for name in ("create_table_query", "uuid", "engine")
        ):
            return self._table_records(scope)
        if table == "system.macros" and "substitution" in fields:
            return self._macro_records(scope)
        recorders = {
            "system.databases": self._database_records,
            "system.replicas": self._replica_records,
            "system.server_settings": self._server_setting_records,
            "system.columns": self._column_records,
        }
        recorder = recorders.get(table)
        return recorder(scope) if recorder is not None else None

    def rows(self, tree, scope, table):
        if tree.find(exp.Count) is not None:
            return None
        records = self._catalog_records(tree, scope, table)
        if records is None:
            return None
        condition = tree.args.get("where")
        if condition is not None:
            records = [record for record in records if _matches(condition.this, record)]
        order = tree.args.get("order")
        if order is not None:
            records.sort(
                key=lambda record: tuple(_project(item.this, record) for item in order.expressions)
            )
        return [tuple(_project(item, record) for item in tree.expressions) for record in records]


def _table(name):
    return parse_one(f"SELECT * FROM {name}", read=ClickHouseDialect).args["from_"].this


def _project(expression, record):
    if isinstance(expression, (exp.Alias, exp.Paren)):
        return _project(expression.this, record)
    if isinstance(expression, exp.Literal):
        return expression.to_py()
    if isinstance(expression, exp.Cast):
        return str(_project(expression.this, record))
    if isinstance(expression, exp.Anonymous) and expression.name.casefold() == "tostring":
        return str(_project(expression.expressions[0], record))
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
    if isinstance(expression, exp.In):
        return _project(expression.this, record) in {
            _project(item, record) for item in expression.expressions
        }
    assert isinstance(expression, exp.EQ), expression
    return _project(expression.this, record) == _project(expression.expression, record)
