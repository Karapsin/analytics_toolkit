"""Stateful DBAPI boundaries for real transfer staging and finalization SQL."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from threading import Barrier, Lock

from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.dml.transfer.flow import (
    adaptive_input,
    api,
    attempt,
    finalize,
    keyed_pipeline,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch
from psycopg2.extensions import adapt
from sqlglot import Dialect, exp, parse_one

from tests.sql._support.load_column_state import ExistingColumnStore
from tests.sql._support.load_table import gp_insert_module


class TransferDatabase:
    def __init__(self, backend, **settings):
        columns = settings.pop("columns", ("id", "payload"))
        rows = settings.pop("rows", None)
        self.backend = backend
        self.dialect = "postgres" if backend == "gp" else "trino"
        target = ExistingColumnStore(
            backend,
            columns,
            not_null=settings.pop("not_null", ("payload",)),
            defaults=settings.pop("defaults", None),
        )
        target.schema = settings.pop("target_types", None) or dict.fromkeys(columns, "BIGINT")
        self.tables = {("sandbox", "events"): target}
        self.source_columns = list(settings.pop("source_columns", columns))
        self.source_types = settings.pop("source_types", None) or dict.fromkeys(
            self.source_columns, "Nullable(Int64)"
        )
        self.writer_barrier = (
            Barrier(2, timeout=5) if settings.pop("synchronize_writers", False) else None
        )
        self.writer_stages = set()
        self.writer_registry_lock = Lock()
        assert not settings
        self.source_rows = [(7, 11), (8, None)] if rows is None else rows
        self.reads = []
        self.commands = []
        self.live_mutations = []
        self.stage_inserts = []
        self.stage_values = []
        self.constraint_probes = []

    @property
    def target(self):
        return self.tables[("sandbox", "events")]

    def identity(self, node):
        if isinstance(node, exp.Schema):
            node = node.this
        return node.db or "sandbox", node.name

    def table(self, node):
        return self.tables[self.identity(node)]

    def metadata(self, statement, params):
        if "to_regclass" in statement:
            table = parse_one(f"SELECT * FROM {params[0]}", read=self.dialect).args["from_"].this
            return [(params[0] if self.identity(table) in self.tables else None,)]
        if "information_schema.tables" in statement:
            return [(1,)] if tuple(params) in self.tables else []
        if (
            "pg_catalog.pg_attribute" not in statement
            and "information_schema.columns" not in statement
        ):
            return None
        return self.column_metadata(statement, self.tables[tuple(params)])

    @staticmethod
    def column_metadata(statement, table):
        if "format_type(" in statement or "data_type" in statement:
            return list(table.schema.items())
        if "is_nullable" in statement:
            return [
                (name, "NO", str(table.defaults[name]) if name in table.defaults else None)
                for name in table.schema
                if name in table.not_null
            ]
        if "attnotnull" in statement:
            return [
                (name, True, name in table.defaults, "", "")
                for name in table.schema
                if name in table.not_null
            ]
        return []

    def execute(self, connection, statement, params=None):
        self.commands.append(statement)
        metadata = self.metadata(statement, params)
        if metadata is not None:
            return metadata, []
        parsed_statement = self._ddl_schema_prefix(statement)
        tree = parse_one(parsed_statement, read=self.dialect)
        if tree is None or isinstance(tree, exp.Analyze):
            return [], []
        if isinstance(tree, exp.Select):
            return self.select(tree)
        target = tree.find(exp.Table)
        if target is None:
            message = f"Unsupported transfer driver command: {statement}"
            raise AssertionError(message)
        identity = self.identity(target)
        if identity == ("sandbox", "events"):
            self.live_mutations.append(statement)
        connection.begin_mutation()
        if isinstance(tree, exp.Create):
            columns = tree.this.expressions
            table = ExistingColumnStore(self.backend, [column.name for column in columns])
            table.schema = {
                column.name: column.kind.sql(dialect=self.dialect) for column in columns
            }
            table.rows = []
            self.tables[identity] = table
        elif isinstance(tree, exp.Drop):
            self.tables.pop(identity, None)
        elif isinstance(tree, (exp.TruncateTable, exp.Delete)):
            table = self.tables[identity]
            table.rows = [
                row for row in table.rows if not self.matches(tree.args.get("where"), row)
            ]
        elif isinstance(tree, exp.Insert):
            self._insert(tree, identity, params)
        else:
            message = f"Unsupported transfer driver statement: {type(tree).__name__}"
            raise TypeError(message)
        return [], []

    def _insert(self, tree, identity, params):
        self._await_private_writer(tree, identity)
        names = [column.name for column in tree.this.expressions]
        if isinstance(tree.expression, exp.Values):
            width = len(names)
            rows = [params[index : index + width] for index in range(0, len(params), width)]
        else:
            rows, _columns = self.select(tree.expression)
        candidate = deepcopy(self.tables[identity])
        candidate.insert(names, rows)
        self.tables[identity] = candidate
        if identity != ("sandbox", "events"):
            self.stage_inserts.append((identity, deepcopy(rows)))
            if isinstance(tree.expression, exp.Values):
                self.stage_values.append((identity, [tuple(row) for row in rows]))

    def _await_private_writer(self, tree, identity):
        if (
            self.writer_barrier is None
            or identity == ("sandbox", "events")
            or not isinstance(tree.expression, exp.Values)
        ):
            return
        with self.writer_registry_lock:
            if identity in self.writer_stages:
                return
            self.writer_stages.add(identity)
        # Wait outside the registry lock, before any row insert, so a second
        # actual writer must own the other key instead of one worker taking both.
        self.writer_barrier.wait()

    def _ddl_schema_prefix(self, statement):
        # SQLGlot's PostgreSQL grammar does not include Greenplum storage and
        # distribution clauses. Parse the complete emitted column definitions;
        # those unrelated physical properties do not affect this driver's rows.
        tokens = Dialect.get_or_raise(self.dialect).tokenize(statement)
        if self.backend != "gp" or not tokens or tokens[0].text.upper() != "CREATE":
            return statement
        depth = 0
        for token in tokens:
            depth += int(token.text == "(") - int(token.text == ")")
            if token.text == ")" and depth == 0:
                return statement[: token.end + 1]
        return statement

    def select(self, tree, outer=None):
        if isinstance(tree, exp.Subquery):
            return self.select(tree.this, outer)
        if isinstance(tree, exp.Union):
            left, columns = self.select(tree.left, outer)
            right, _ = self.select(tree.right, outer)
            return left + right, columns
        source = tree.args["from_"].this
        if isinstance(source, exp.Subquery):
            values, columns = self.select(source.this, outer)
            rows = [dict(zip(columns, row)) for row in values]
        else:
            table = self.table(source)
            rows, columns = table.rows, list(table.schema)
        alias = source.alias_or_name
        rows = [
            {**(outer or {}), **row, **{f"{alias}.{name}": value for name, value in row.items()}}
            for row in rows
        ]
        rows = [row for row in rows if self.matches(tree.args.get("where"), row)]
        expressions = tree.expressions
        if group := tree.args.get("group"):
            counts = Counter(
                tuple(self.value(column, row) for column in group.expressions) for row in rows
            )
            rows = [
                {
                    **dict(zip((column.name for column in group.expressions), key)),
                    "__count__": count,
                }
                for key, count in counts.items()
            ]
            rows = [row for row in rows if self.matches(tree.args.get("having"), row)]
        elif expressions[0].find(exp.Count) is not None:
            if any(expression.find(exp.Sub) is not None for expression in expressions):
                self.constraint_probes.append(
                    (columns, [tuple(row[name] for name in columns) for row in rows])
                )
            return [tuple(self.aggregate(expression, rows) for expression in expressions)], [
                expression.alias_or_name for expression in expressions
            ]
        selected_columns = (
            columns
            if isinstance(expressions[0], exp.Star)
            else [expression.alias_or_name for expression in expressions]
        )
        values = [
            tuple(row[name] for name in columns)
            if isinstance(expressions[0], exp.Star)
            else tuple(self.value(expression, row) for expression in expressions)
            for row in rows
        ]
        if tree.args.get("distinct"):
            values = list(dict.fromkeys(values))
        limit = tree.args.get("limit")
        return (values[: int(limit.expression.this)] if limit else values), selected_columns

    def aggregate(self, node, rows):
        if isinstance(node, exp.Alias):
            return self.aggregate(node.this, rows)
        if isinstance(node, exp.Sub):
            return self.aggregate(node.left, rows) - self.aggregate(node.right, rows)
        if isinstance(node, exp.Count):
            return (
                len(rows)
                if isinstance(node.this, exp.Star)
                else sum(self.value(node.this, row) is not None for row in rows)
            )
        message = f"Unsupported transfer aggregate: {type(node).__name__}"
        raise AssertionError(message)

    def matches(self, condition, row):
        if condition is None:
            matched = True
        elif isinstance(condition, (exp.Where, exp.Having, exp.Paren)):
            matched = self.matches(condition.this, row)
        elif isinstance(condition, exp.And):
            matched = self.matches(condition.left, row) and self.matches(condition.right, row)
        elif isinstance(condition, exp.Or):
            matched = self.matches(condition.left, row) or self.matches(condition.right, row)
        elif isinstance(condition, exp.Is):
            matched = self._matches_is(condition, row)
        elif isinstance(condition, (exp.EQ, exp.NullSafeEQ)):
            matched = self.value(condition.left, row) == self.value(condition.right, row)
        elif isinstance(condition, exp.GT):
            matched = self.value(condition.left, row) > self.value(condition.right, row)
        elif isinstance(condition, exp.Not):
            matched = not self.matches(condition.this, row)
        elif isinstance(condition, exp.Exists):
            matched = bool(self.select(condition.this, row)[0])
        else:
            message = f"Unsupported transfer filter: {type(condition).__name__}"
            raise AssertionError(message)
        return matched

    def _matches_is(self, condition, row):
        matched = self.value(condition.left, row) is self.value(condition.right, row)
        return not matched if condition.args.get("negate") else matched

    def value(self, node, row):
        if isinstance(node, (exp.Alias, exp.Cast, exp.Paren)):
            return self.value(node.this, row)
        if isinstance(node, exp.Column):
            return row[f"{node.table}.{node.name}" if node.table else node.name]
        if isinstance(node, exp.Count):
            return row["__count__"]
        if isinstance(node, exp.Null):
            return None
        if isinstance(node, exp.Literal):
            return node.this if node.is_string else int(node.this)
        message = f"Unsupported transfer value: {type(node).__name__}"
        raise AssertionError(message)


class TransferConnection:
    server_tz = "UTC"
    effective_read_timezone = "UTC"

    def __init__(self, database):
        self.database = database
        self.snapshot = None

    def cursor(self):
        return TransferCursor(self)

    def begin_mutation(self):
        if self.database.backend == "gp" and self.snapshot is None:
            self.snapshot = deepcopy(self.database.tables)

    def commit(self):
        self.snapshot = None

    def rollback(self):
        if self.snapshot is not None:
            self.database.tables = self.snapshot
            self.snapshot = None

    def close(self):
        pass


class TransferCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []
        self.description = None
        self.rowcount = -1

    def execute(self, statement, params=None):
        self.rows, columns = self.connection.database.execute(self.connection, statement, params)
        self.description = [(name, None, None, None, None, None, None) for name in columns]
        self.rowcount = len(self.rows)

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return self.rows

    def close(self):
        pass


def install_transfer_database(monkeypatch, backend, **settings):
    database = TransferDatabase(backend, **settings)

    def connect(_key):
        return TransferConnection(database)

    def inspect(_backend, _connection, _query):
        return [SourceColumn(name, database.source_types[name]) for name in database.source_columns]

    def read(_key, _backend, _ref, query, *_args, **_kwargs):
        database.reads.append(query)
        tree = parse_one(query, read="clickhouse")
        for row in database.source_rows:
            if database.matches(tree.args.get("where"), dict(zip(database.source_columns, row))):
                yield RowBatch(database.source_columns, [row])

    def execute_values(cursor, statement, rows, page_size, **_kwargs):
        del page_size
        for row in rows:
            for value in row:
                adapt(value).getquoted()
        placeholders = ", ".join("(" + ", ".join("NULL" for _ in row) + ")" for row in rows)
        cursor.execute(statement % placeholders, [value for row in rows for value in row])

    for module in (attempt, api, finalize, keyed_pipeline):
        monkeypatch.setattr(module, "get_sql_connection", connect)
    monkeypatch.setattr(attempt, "inspect_source_query_schema", inspect)
    monkeypatch.setattr(attempt, "iter_source_batches", read)
    monkeypatch.setattr(adaptive_input, "iter_source_batches", read)
    monkeypatch.setattr(keyed_pipeline, "iter_source_batches", read)
    monkeypatch.setattr(gp_insert_module, "execute_values", execute_values)
    return database
