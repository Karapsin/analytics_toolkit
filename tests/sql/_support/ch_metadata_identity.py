"""Exact namespace catalogs independent of ClickHouse metadata normalization."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one


def _table(database, relation):
    return exp.Table(
        this=exp.to_identifier(relation, quoted=True),
        db=exp.to_identifier(database, quoted=True),
    )


def _table_sql(database, relation):
    return _table(database, relation).sql(dialect=ClickHouseDialect)


def _identity(table):
    if isinstance(table, str):
        table = parse_one(f"SELECT * FROM {table}", read=ClickHouseDialect).find(exp.Table)
    assert isinstance(table, exp.Table)
    return table.db, table.name


class NamespaceClient:
    """A single real-shaped endpoint with distinct facade and storage catalogs."""

    def __init__(self, facade_database, storage_database):
        assert facade_database != storage_database
        self.facade_database = facade_database
        self.storage_database = storage_database
        self.facade_tableSQL = _table_sql(facade_database, "events")
        self.storage_tableSQL = _table_sql(storage_database, "events_shard")
        self.unrelated_tableSQL = _table_sql(facade_database, "events_shard")
        engine = exp.Anonymous(
            this="Distributed",
            expressions=[
                exp.Literal.string("core"),
                exp.Literal.string(storage_database),
                exp.Literal.string("events_shard"),
                exp.Anonymous(this="rand"),
            ],
        )
        self.engineSQL = engine.sql(dialect=ClickHouseDialect)
        decoded = parse_one(f"SELECT {self.engineSQL}", read=ClickHouseDialect).expressions[0]
        assert isinstance(decoded, exp.Anonymous)
        database_argument = decoded.expressions[1]
        assert isinstance(database_argument, exp.Literal)
        assert database_argument.is_string
        assert database_argument.this == storage_database
        self.facade_ddl = f"CREATE TABLE {self.facade_tableSQL} (id UInt64) ENGINE={self.engineSQL}"
        self.resources = {
            (facade_database, "events"): SimpleNamespace(
                uuid=uuid4(),
                ddl=self.facade_ddl,
                rows=[],
                route=(database_argument.this, decoded.expressions[2].this),
            ),
            (storage_database, "events_shard"): SimpleNamespace(
                uuid=uuid4(),
                ddl=(
                    f"CREATE TABLE {self.storage_tableSQL} (id UInt64) ENGINE=MergeTree ORDER BY id"
                ),
                rows=[(1,), (2,), (3,)],
                route=None,
            ),
            (facade_database, "events_shard"): SimpleNamespace(
                uuid=uuid4(),
                ddl=(
                    f"CREATE TABLE {self.unrelated_tableSQL} (id UInt64) "
                    "ENGINE=MergeTree ORDER BY id"
                ),
                rows=[(value,) for value in range(1001, 1101)],
                route=None,
            ),
        }
        self.commands = []
        self.queries = []
        self.query_settings = []
        self.closed = False
        self.server_uuid = uuid4()

    def command(self, statement, settings=None):
        del settings
        self.commands.append(statement)
        message = "Namespace identity tests must not submit data or structural changes."
        raise AssertionError(message)

    def query(self, statement, settings=None):
        self.queries.append(statement)
        self.query_settings.append(deepcopy(settings))
        if statement.startswith("SHOW CREATE TABLE "):
            resource = self.resources[_identity(statement[len("SHOW CREATE TABLE ") :])]
            rows = [(resource.ddl,)]
        elif statement.startswith("EXISTS TABLE "):
            rows = [(int(_identity(statement[len("EXISTS TABLE ") :]) in self.resources),)]
        else:
            rows = self._select(parse_one(statement, read=ClickHouseDialect))
        return SimpleNamespace(result_rows=rows)

    def _select(self, select):
        source = select.args.get("from_")
        if source is None:
            record = {
                "serverUUID": self.server_uuid,
                "tcpPort": 9000,
                "currentDatabase": self.facade_database,
                "getMacro": "core",
            }
            return [tuple(_project(expression, record) for expression in select.expressions)]
        table = source.this
        if isinstance(table.this, exp.Anonymous):
            function = table.this
            assert function.name.casefold() in {"cluster", "clusterallreplicas"}
            cluster, database, relation = (argument.name for argument in function.expressions)
            assert cluster == "core"
        else:
            database, relation = _identity(table)
        if database != "system":
            resource = self.resources[(database, relation)]
            physical = self.resources[resource.route] if resource.route is not None else resource
            return [(len(physical.rows),)]
        records = self._catalog(relation)
        condition = select.args.get("where")
        if condition is not None:
            records = [record for record in records if _matches(condition.this, record)]
        if any(isinstance(expression, exp.Count) for expression in select.expressions):
            return [(len(records),)]
        return [
            tuple(_project(expression, record) for expression in select.expressions)
            for record in records
        ]

    def _catalog(self, relation):
        if relation == "databases":
            return [
                {"name": database, "engine": "Atomic"}
                for database in (self.facade_database, self.storage_database)
            ]
        if relation == "clusters":
            return [
                {
                    "_shard_num": 1,
                    "cluster": "core",
                    "shard_num": 1,
                    "replica_num": 1,
                    "host_name": "coordinator",
                    "host_address": "10.0.0.1",
                    "port": 9000,
                    "is_local": 1,
                }
            ]
        if relation == "one":
            return [{"_shard_num": 1, "serverUUID": self.server_uuid, "tcpPort": 9000}]
        assert relation in {"tables", "columns"}
        return [
            {
                "_shard_num": 1,
                "database": database,
                "name": table if relation == "tables" else "id",
                "table": table,
                "type": "UInt64",
                "engine": "Distributed" if resource.route is not None else "MergeTree",
                "create_table_query": resource.ddl,
                "uuid": resource.uuid,
            }
            for (database, table), resource in self.resources.items()
        ]

    def close(self):
        self.closed = True


EscapedNamespaceClient = NamespaceClient


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
    if isinstance(expression, exp.In):
        return record[expression.this.name] in [item.to_py() for item in expression.expressions]
    assert isinstance(expression, exp.EQ)
    return record[expression.this.name] == expression.expression.to_py()
