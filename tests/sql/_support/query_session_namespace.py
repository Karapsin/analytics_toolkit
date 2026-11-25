"""GP sessions resolve unqualified relations after their own native SET statements."""

from __future__ import annotations

from analytics_toolkit.sql.backends import get_backend_adapter
from psycopg2 import extensions
from sqlglot import exp, parse_one

from tests.sql._support.created_gp_relations import locked_relation
from tests.sql._support.load_column_state import ExistingColumnStore
from tests.sql._support.query_native_assignment import (
    QueryNativeConnection,
    QueryNativeCursor,
    QueryNativeDatabase,
    adaptive_input,
    attempt,
    create,
    query_native_assignment,
    transfer_retry,
)
from tests.sql._support.query_snapshot_sql import is_zero_limit_query


class SessionDatabase(QueryNativeDatabase):
    def __init__(self):
        super().__init__([(1,), (2,)], native_type="BIGINT", exists=False)
        self.tables.clear()
        for identity, values in {
            ("archive", "source_rows"): [1, 2],
            ("public", "events"): list(range(1000, 1100)),
        }.items():
            table = ExistingColumnStore("gp", ["payload"], not_null=())
            table.schema = {"payload": "BIGINT"}
            table.rows = [{"payload": value} for value in values]
            self.tables[identity] = table
        self.execution_connection = None
        self.setup_sessions = []
        self.selected_reads = 0

    def identity(self, node):
        if isinstance(node, exp.Schema):
            node = node.this
        schema = getattr(self.execution_connection, "current_schema", "public")
        return node.db or schema, node.name

    def execute(self, connection, statement, params=None):
        previous = self.execution_connection
        self.execution_connection = connection
        try:
            if locked_relation(statement) is not None:
                return super().execute(connection, statement, params)
            tree = parse_one(self._ddl_schema_prefix(statement), read="postgres")
            if isinstance(tree, exp.Set):
                assignment = tree.find(exp.EQ)
                assert assignment.this.name.lower() == "search_path"
                connection.current_schema = assignment.expression.name
                self.commands.append(statement)
                self.setup_sessions.append(connection)
                self.result_types = []
                return [], []
            if "current_schema()" in statement:
                self.commands.append(statement)
                self.result_types = ["TEXT"]
                return [(connection.current_schema,)], ["current_schema"]
            return super().execute(connection, statement, params)
        finally:
            self.execution_connection = previous

    def select(self, tree, outer=None):
        source = tree.args.get("from_") if isinstance(tree, exp.Select) else None
        if (
            source is not None
            and isinstance(source.this, exp.Table)
            and tree.find(exp.Count) is None
            and not self.counting_source
            and not is_zero_limit_query(tree)
            and self.identity(source.this) in {("archive", "source_rows"), ("archive", "events")}
        ):
            self.selected_reads += 1
        return super().select(tree, outer)


class SessionConnection(QueryNativeConnection):
    current_schema = "public"


def install_session_query(monkeypatch):
    database = SessionDatabase()

    def register_type(_caster, cursor):
        assert isinstance(cursor, QueryNativeCursor)

    monkeypatch.setattr(extensions, "register_type", register_type)

    def connect(alias):
        connection = SessionConnection(database, alias)
        database.opened.append(connection)
        return connection

    for module in (create, adaptive_input, attempt, transfer_retry, query_native_assignment):
        monkeypatch.setattr(module, "get_sql_connection", connect)
    monkeypatch.setattr(
        get_backend_adapter("gp"),
        "open_connection",
        lambda config, **_kwargs: connect(config.connection_key),
    )
    return database
