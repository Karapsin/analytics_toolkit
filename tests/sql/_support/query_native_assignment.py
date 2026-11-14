"""Native query creation over stateful GP SQL and real parameter adaptation."""

from __future__ import annotations

import importlib
from copy import deepcopy

from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.dml import query_native_assignment
from analytics_toolkit.sql.dml.transfer.flow import adaptive_input, attempt
from analytics_toolkit.sql.dml.transfer.runtime import retry as transfer_retry
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch
from sqlglot import Dialect, exp, parse_one

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.gp_temporal_values import TemporalCursor
from tests.sql._support.load_column_state import ExistingColumnStore
from tests.sql._support.transfer_native_assignment import (
    NativeTransferConnection,
    NativeTransferDatabase,
)

create = importlib.import_module("analytics_toolkit.sql.dml.table.create_table_from_sql")


class QueryNativeDatabase(NativeTransferDatabase):
    def __init__(self, rows, *, native_type="TEXT", exists=True):
        super().__init__(
            rows=rows,
            target_schema={"payload": native_type},
            source_types={"payload": native_type},
            exists=exists,
        )
        self.source_evaluations = []
        self.snapshot_creates = []
        self.result_types = []
        self.last_insert_count = 0
        self.query_inspections = []
        self.python_reads = []
        self.python_selected_rows = []
        self.closed = []
        self.snapshot_header_rename = None
        self.snapshot_headers_seen = []
        self.private_query_attempts = []
        self.fail_private_query_assignments = 0
        self.private_failure_live_mutations = []
        self.reject_source_reexecution = False
        source = ExistingColumnStore("gp", ["payload"], not_null=())
        source.schema = {"payload": native_type}
        source.rows = [{"payload": row[0]} for row in rows]
        self.tables[("sandbox", "source_rows")] = source
        if exists:
            self.target.rows = deepcopy(source.rows)

    def _ddl_schema_prefix(self, statement):
        tokens = Dialect.get_or_raise(self.dialect).tokenize(statement)
        # GP distribution is outside SQLGlot's PostgreSQL grammar. Keep every
        # query expression, including CAST parentheses, before that clause.
        if tokens and tokens[0].text.upper() == "CREATE":
            for token in tokens:
                if token.text.upper() == "DISTRIBUTED":
                    statement = statement[: token.start].rstrip()
                    break
            first_open = next(
                (index for index, token in enumerate(tokens) if token.text == "("), None
            )
            first_as = next(
                (index for index, token in enumerate(tokens) if token.text.upper() == "AS"), None
            )
            if first_open is not None and (first_as is None or first_open < first_as):
                return super()._ddl_schema_prefix(statement)
        return statement

    def execute(self, connection, statement, params=None):
        tree = parse_one(self._ddl_schema_prefix(statement), read="postgres")
        if isinstance(tree, exp.Create) and tree.expression is not None:
            self.commands.append(statement)
            connection.begin_mutation()
            rows, names = self.select(tree.expression)
            table = ExistingColumnStore("gp", names, not_null=())
            table.schema = dict(zip(names, self.query_types(tree.expression)))
            table.rows = [dict(zip(names, row)) for row in rows]
            identity = self.identity(tree.this)
            self.tables[identity] = table
            self.snapshot_creates.append(identity)
            self.result_types = []
            return [], []
        rows, names = super().execute(connection, statement, params)
        self.result_types = self.query_types(tree) if isinstance(tree, exp.Select) and names else []
        return rows, names

    def select(self, tree, outer=None):
        if isinstance(tree, exp.Subquery):
            return self.select(tree.this, outer)
        if isinstance(tree, exp.Select):
            condition = tree.args.get("where")
            if (
                condition is not None
                and isinstance(condition.this, exp.EQ)
                and isinstance(condition.this.left, exp.Literal)
                and isinstance(condition.this.right, exp.Literal)
                and condition.this.left.this != condition.this.right.this
            ):
                self.query_inspections.append(tree.sql(dialect="postgres"))
                return [], self.query_names(tree)
            source = tree.args["from_"].this
            is_probe = tree.find(exp.Table) is not None and any(
                table.name in {"source_rows", "events"} for table in tree.find_all(exp.Table)
            )
            if not isinstance(source, exp.Subquery) and self.identity(source) in {
                ("sandbox", "events"),
                ("sandbox", "source_rows"),
            }:
                assert not self.reject_source_reexecution, "Certified query source was rerun."
                self.source_evaluations.append(tree.sql(dialect="postgres"))
            elif is_probe and source.alias == "source_schema_probe":
                self.query_inspections.append(tree.sql(dialect="postgres"))
        return super().select(tree, outer)

    def query_names(self, tree):
        if isinstance(tree, exp.Subquery):
            return self.query_names(tree.this)
        if not isinstance(tree.expressions[0], exp.Star):
            return [item.alias_or_name for item in tree.expressions]
        source = tree.args["from_"].this
        return (
            self.query_names(source)
            if isinstance(source, exp.Subquery)
            else list(self.table(source).schema)
        )

    def query_types(self, tree):
        if isinstance(tree, exp.Subquery):
            return self.query_types(tree.this)
        source = tree.args["from_"].this
        if isinstance(source, exp.Subquery):
            source_schema = dict(
                zip(
                    self.query_names(source),
                    self.query_types(source),
                )
            )
        else:
            source_schema = self.table(source).schema
        if isinstance(tree.expressions[0], exp.Star):
            return list(source_schema.values())
        result = []
        for expression in tree.expressions:
            value = expression.this if isinstance(expression, exp.Alias) else expression
            if isinstance(value, exp.Cast):
                result.append(value.args["to"].sql(dialect="postgres"))
            elif isinstance(value, exp.Column):
                result.append(source_schema[value.name])
            else:
                result.append("BIGINT")
        return result

    def _insert(self, tree, identity, params):
        if identity != ("sandbox", "events") and not isinstance(tree.expression, exp.Values):
            self.private_query_attempts.append(identity)
            if self.fail_private_query_assignments:
                self.fail_private_query_assignments -= 1
                self.private_failure_live_mutations.append(deepcopy(self.live_mutations))
                self.reject_source_reexecution = True
                message = "Transient failure before private native query assignment."
                raise ConnectionError(message)
        count_before = len(self.tables[identity].rows)
        super()._insert(tree, identity, params)
        self.last_insert_count = len(self.tables[identity].rows) - count_before


class QueryNativeCursor(TemporalCursor):
    def execute(self, statement, params=None):
        super().execute(statement, params)
        database = self.connection.database
        self.description = [
            (name, native_type, None, None, None, None, None)
            for (name, *_rest), native_type in zip(self.description or [], database.result_types)
        ]
        text = statement.decode("utf-8") if isinstance(statement, bytes) else statement
        tree = parse_one(database._ddl_schema_prefix(text), read="postgres")
        if database.snapshot_header_rename and isinstance(tree, exp.Select):
            source = tree.args.get("from_")
            if (
                source is not None
                and isinstance(source.this, exp.Subquery)
                and any(
                    database.identity(table) in database.snapshot_creates
                    for table in source.this.find_all(exp.Table)
                )
            ):
                original = self.description[0]
                renamed = (database.snapshot_header_rename, *original[1:])
                database.snapshot_headers_seen.append((original[0], renamed[0]))
                self.description[0] = renamed
        if isinstance(tree, exp.Insert):
            self.rowcount = database.last_insert_count

    def fetchmany(self, size):
        rows, self.rows = self.rows[:size], self.rows[size:]
        return rows


class QueryNativeConnection(NativeTransferConnection):
    def __init__(self, database, alias):
        super().__init__(database)
        self.alias = alias

    def cursor(self):
        return QueryNativeCursor(self)

    def close(self):
        self.database.closed.append(self)


def install_query_native(monkeypatch, rows, *, native_type="TEXT", exists=True):
    database = QueryNativeDatabase(rows, native_type=native_type, exists=exists)
    databases = {"gp": database, "gp_sandbox": database, "ch": database}

    def connect(alias):
        connection = QueryNativeConnection(databases[alias], alias)
        databases[alias].opened.append(connection)
        return connection

    def read(_key, _backend, _ref, query, *_args, **_kwargs):
        database.python_reads.append(query)
        for row in database.source_rows:
            database.python_selected_rows.append(row)
            yield RowBatch(["payload"], [row])

    for module in (create, adaptive_input, attempt, transfer_retry, query_native_assignment):
        monkeypatch.setattr(module, "get_sql_connection", connect)
    monkeypatch.setattr(adaptive_input, "iter_source_batches", read)
    monkeypatch.setattr(attempt, "iter_source_batches", read)
    return database, databases, connect


def install_python_query_native(monkeypatch, write_sql_connections, rows):
    write_sql_connections(
        {
            **DEFAULT_SQL_CONNECTIONS,
            "ch": {**DEFAULT_SQL_CONNECTIONS["ch"], "transfer_staging_schema": None},
        }
    )
    database, _databases, _connect = install_query_native(monkeypatch, rows)
    monkeypatch.setattr(
        create, "inspect_source_query_schema", lambda *_args: [SourceColumn("payload", "String")]
    )
    return database


def truncate_python_checkpoint(monkeypatch):
    complete_batches = attempt.resolved_source_batches

    def truncated_batches(*args, **kwargs):
        for batch in complete_batches(*args, **kwargs):
            if batch.rows:
                yield RowBatch(batch.columns, batch.rows[:1])
                return

    monkeypatch.setattr(attempt, "resolved_source_batches", truncated_batches)
