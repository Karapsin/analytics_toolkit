"""Native metadata at the legacy query-contract fixture's external I/O boundary."""

from __future__ import annotations

from types import SimpleNamespace

import psycopg2.extensions
from analytics_toolkit.sql.backends.ch.creation_policy import ClickHouseCreationPolicy
from analytics_toolkit.sql.connection.config import get_connection_config
from sqlglot import Dialect, exp, parse_one

from tests.sql._support.ch_drop_scope import DropScopeState
from tests.sql._support.ch_lifecycle import LifecycleConnection
from tests.sql._support.create_from_sql import FakeDbapiConnection, FakeDbapiCursor
from tests.sql._support.created_gp_relations import relation_catalog_result


class CatalogCursor(FakeDbapiCursor):
    def __init__(self, connection):
        super().__init__(connection)
        self.rows = []

    def execute(self, sql, params=None):
        text = sql.decode("utf-8") if isinstance(sql, bytes) else sql
        super().execute(text, params)
        self.read_offset = 0
        if self._session_query(text, params):
            return
        catalog = relation_catalog_result(self.connection, self.connection, text, params)
        if catalog is not None:
            self.rows, names = catalog
            self.description = [(name, 25, None, None, None, None) for name in names]
            return
        self._statement(parse_one(_without_distribution(text), read="postgres"))

    def _session_query(self, text, params):
        connection = self.connection
        if text.strip() == "SELECT to_regclass(%s)":
            table = parse_one(f"SELECT * FROM {params[0]}", read="postgres").args["from_"].this
            self.rows = [(params[0] if connection.identity(table) in connection.tables else None,)]
            self.description = [("to_regclass", 25, None, None, None, None)]
            return True
        if "information_schema.tables" in text and params is not None:
            schema, relation = params
            self.rows = [(1,)] if (schema, relation) in connection.tables else []
            self.description = [("exists", 23, None, None, None, None)]
            return True
        if text.strip() == "SELECT current_catalog, current_schema":
            self.rows = [(get_connection_config(connection.alias).catalog, "sandbox")]
            self.description = [
                (name, 25, None, None, None, None) for name in ("catalog", "schema")
            ]
            return True
        return False

    def _statement(self, tree):
        connection = self.connection
        if isinstance(tree, exp.Create):
            target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
            columns = tree.this.expressions if isinstance(tree.this, exp.Schema) else []
            schema = {
                column.name: column.args["kind"].sql(dialect="postgres")
                for column in columns
                if isinstance(column, exp.ColumnDef)
            }
            connection.tables[connection.identity(target)] = SimpleNamespace(schema=schema, rows=[])
            self.rows, self.description = [], None
        elif isinstance(tree, exp.Drop):
            for target in tree.args.get("tables") or [tree.this]:
                connection.tables.pop(connection.identity(target), None)
            self.rows, self.description = [], None
        elif isinstance(tree, exp.Select):
            self._select(tree)
        else:
            self.rows, self.description = [], None

    def _select(self, tree):
        self.rows = list(self.connection.source_rows)
        self.description = self.connection.description
        if tree.find(exp.Count) is not None:
            self.rows = [(len(self.rows),)]
            self.description = [("count", 20, None, None, None, None)]
        elif tree.args.get("where") is not None or tree.args.get("limit") is not None:
            self.rows = []

    def mogrify(self, statement, values=None):
        text = statement.decode("utf-8") if isinstance(statement, bytes) else statement
        for value in values or ():
            adapted = psycopg2.extensions.adapt(value)
            quoted = adapted.getquoted().decode("utf-8")
            text = text.replace("%s", quoted, 1)
        return text.encode("utf-8")

    def fetchmany(self, size):
        rows = self.rows[self.read_offset : self.read_offset + size]
        self.read_offset += len(rows)
        return rows

    def fetchall(self):
        return self.fetchmany(len(self.rows))

    def fetchone(self):
        rows = self.fetchmany(1)
        return rows[0] if rows else None


def _without_distribution(statement):
    tokens = Dialect.get_or_raise("postgres").tokenize(statement)
    if tokens and tokens[0].text.upper() == "CREATE":
        for token in tokens:
            if token.text.upper() == "DISTRIBUTED":
                return statement[: token.start].rstrip()
    return statement


class CatalogConnection(FakeDbapiConnection):
    def __init__(self, *args, **kwargs):
        state = kwargs.pop("state", None)
        alias = kwargs.pop("alias", "gp")
        super().__init__(*args, **kwargs)
        self.alias = alias
        self.catalog_state = state or SimpleNamespace(
            tables={}, next_relation_oid=10000, namespace_oids={}
        )
        self.tables = self.catalog_state.tables
        self.namespace_oids = self.catalog_state.namespace_oids
        self.result_types = []
        self.commands = []

    @property
    def next_relation_oid(self):
        return self.catalog_state.next_relation_oid

    @next_relation_oid.setter
    def next_relation_oid(self, value):
        self.catalog_state.next_relation_oid = value

    @staticmethod
    def identity(table):
        return table.db or "public", table.name

    def cursor(self):
        cursor = CatalogCursor(self)
        self.cursors.append(cursor)
        return cursor


def install_fake_type_registration(monkeypatch):
    def register(caster, cursor):
        assert isinstance(cursor, CatalogCursor)
        assert caster.values

    def unexpected_connect(*_args, **_kwargs):
        message = "Query contract escaped its fake DBAPI connection boundary."
        raise AssertionError(message)

    monkeypatch.setattr(psycopg2.extensions, "register_type", register)
    monkeypatch.setattr(psycopg2, "connect", unexpected_connect)


class CatalogClickHouseClient(LifecycleConnection):
    def __init__(self, state=None):
        state = state or empty_clickhouse_catalog()
        super().__init__(state, state.coordinator, native=False)

    @property
    def commands(self):
        return self.state.commands

    @property
    def data_inserts(self):
        return [(item["table"], item["columns"], item["rows"]) for item in self.state.inserts]

    def _insert(self, target, rows, columns=None):
        rows = list(rows)
        super()._insert(target, rows, columns)
        self._written_rows = len(rows)

    def command(self, statement, settings=None):
        self._written_rows = None
        result = super().command(statement, settings=settings)
        return {"written_rows": self._written_rows} if self._written_rows is not None else result


def empty_clickhouse_catalog():
    state = DropScopeState()
    for tables in state.host_tables.values():
        tables.clear()
    state.clusters["core"] = (state.coordinator,)
    # getMacro('cluster') and ON CLUSTER '{cluster}' address the same hosts.
    state.clusters["{cluster}"] = state.clusters["storage"]
    return state


def execute_create_pair():
    """Seven source rows and a local managed pair for the actual CREATE executor."""
    connection = CatalogClickHouseClient()
    source = parse_one(
        "CREATE TABLE analytics.source_rows (id UInt8) ENGINE=Memory", read="clickhouse"
    )
    connection.state._create(connection.host, source, [(value,) for value in range(7)])
    policy = ClickHouseCreationPolicy(
        create_distributed_pair=True,
        shard_engine="Memory",
        shard_on_cluster=None,
        distributed_engine_template=(
            "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
        ),
        distributed_cluster="core",
        distributed_on_cluster=None,
        sharding_key="rand()",
        ddl_ready_timeout_seconds=0.01,
    )
    creates = [
        "CREATE TABLE analytics.target_shard (id UInt8) ENGINE=Memory",
        "CREATE TABLE analytics.target (id UInt8) "
        "ENGINE=Distributed('core','analytics','target_shard',rand())",
    ]
    return connection, policy, creates
