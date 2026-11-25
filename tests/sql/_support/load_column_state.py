"""Stateful transport boundaries for existing-target dataframe column contracts."""

from __future__ import annotations

from types import SimpleNamespace

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.load_table import gp_insert_module, load_df_module


class ExistingColumnStore:
    def __init__(self, backend, columns, **settings):
        self.backend = backend
        self.kinds = settings.pop("kinds", None) or {}
        self.native = settings.pop("native", False)
        self.materialized_allowed = settings.pop("materialized_allowed", False)
        self.not_null = set(settings.pop("not_null", ()))
        self.defaults = settings.pop("defaults", None) or {}
        assert not settings
        self.next_identity = 1000
        self.schema = {
            name: ("Nullable(Int64)" if name == "value" else "Int64")
            if backend == "ch"
            else ("INTEGER" if backend == "gp" else "BIGINT")
            for name in columns
        }
        self.rows = [
            {name: 101 + index for index, name in enumerate(columns)},
            {name: 201 + index for index, name in enumerate(columns)},
        ]
        for row in self.rows:
            for name, kind in self.kinds.items():
                if kind == "EPHEMERAL":
                    row.pop(name, None)
                elif kind in {"GENERATED", "MATERIALIZED", "ALIAS"}:
                    row[name] = row["id"] * 2
        self.submissions = []
        self.metadata_queries = []

    def execute(self, statement, params=None):
        if "information_schema.columns" in statement:
            assert tuple(params) == ("sandbox", "events")
            self.metadata_queries.append(statement)
            if "is_nullable" in statement:
                return [
                    (
                        name,
                        "NO" if name in self.not_null else "YES",
                        str(self.defaults[name]) if name in self.defaults else None,
                    )
                    for name in self.schema
                    if name in self.not_null or "is_nullable = 'NO'" not in statement
                ]
            return list(self.schema.items())
        if "pg_catalog.pg_attribute" in statement:
            assert tuple(params) == ("sandbox", "events")
            self.metadata_queries.append(statement)
            if "pg_catalog.format_type(" in statement:
                return [
                    (name, native)
                    for name, native in self.schema.items()
                    if "attgenerated" not in statement
                    or self.kinds.get(name) not in {"GENERATED", "IDENTITY_ALWAYS"}
                ]
            if "a.attnotnull" in statement:
                return [
                    (
                        name,
                        name in self.not_null,
                        name in self.defaults,
                        "s" if kind == "GENERATED" else "",
                        "a"
                        if kind == "IDENTITY_ALWAYS"
                        else "d"
                        if kind == "IDENTITY_DEFAULT"
                        else "",
                    )
                    for name in self.schema
                    if (kind := self.kinds.get(name, "")) in {"GENERATED", "IDENTITY_ALWAYS"}
                    or name in self.not_null
                ]
            return [
                (name,)
                for name, kind in self.kinds.items()
                if kind in {"GENERATED", "IDENTITY_ALWAYS"}
            ]
        dialect = (
            ClickHouseDialect
            if self.backend == "ch"
            else ("postgres" if self.backend == "gp" else "trino")
        )
        tree = parse_one(statement, read=dialect)
        if isinstance(tree, (exp.Delete, exp.TruncateTable)):
            self.submissions.append(("CLEAR", statement))
            self.rows.clear()
        elif isinstance(tree, (exp.Create, exp.Drop)):
            self.submissions.append(("CREATE_OR_DROP", statement))
        elif isinstance(tree, exp.Insert):
            names = [column.name for column in tree.this.expressions]
            width = len(names)
            rows = [params[index : index + width] for index in range(0, len(params), width)]
            self.insert(names, rows)
        return None

    def insert(self, names, rows):
        self.submissions.append(("INSERT", tuple(names)))
        if len(set(names)) != len(names):
            message = "Server rejected duplicate INSERT column names."
            raise ValueError(message)
        if set(names) - self.schema.keys():
            message = "Server rejected unknown INSERT column extra."
            raise ValueError(message)
        forbidden = [
            name
            for name in names
            if self.kinds.get(name) in {"GENERATED", "IDENTITY_ALWAYS", "ALIAS"}
            or (
                self.kinds.get(name) == "MATERIALIZED"
                and not (self.native and self.materialized_allowed)
            )
        ]
        if forbidden:
            message = "Server rejected noninsertable column calc."
            raise ValueError(message)
        for values in rows:
            row = dict.fromkeys(name for name in self.schema if self.kinds.get(name) != "EPHEMERAL")
            row.update(
                (name, value)
                for name, value in zip(names, values)
                if self.kinds.get(name) != "EPHEMERAL"
            )
            self._populate_omitted_columns(row, names)
            if any(row[name] is None for name in self.not_null):
                message = "Server rejected NULL in a NOT NULL column."
                raise ValueError(message)
            self.rows.append(row)

    def _populate_omitted_columns(self, row, names):
        for name, kind in self.kinds.items():
            if name in names:
                continue
            if kind in {"GENERATED", "MATERIALIZED", "ALIAS"}:
                row[name] = row["id"] * 2
            elif kind in {"IDENTITY_ALWAYS", "IDENTITY_DEFAULT"}:
                row[name] = self.next_identity
                self.next_identity += 1
        for name, value in self.defaults.items():
            if name not in names:
                row[name] = value


class ColumnCursor:
    def __init__(self, store):
        self.store = store
        self.rowcount = -1
        self.description = None
        self.rows = []

    def execute(self, statement, params=None):
        self.rows = self.store.execute(statement, params) or []
        self.rowcount = 0

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class ColumnConnection:
    def __init__(self, store):
        self.store = store
        # Public ClickHouse factories use OutageClient, whose existing-target
        # preparation does not issue a redundant CREATE/readiness wait.
        self._ch_outage_client = store.backend == "ch"
        self.is_native_transport = store.native

    def cursor(self):
        return ColumnCursor(self.store)

    def command(self, statement, **_kwargs):
        self.store.execute(statement)

    def query(self, statement):
        if statement.startswith("SHOW CREATE TABLE"):
            definitions = []
            for name, native in self.store.schema.items():
                kind = self.store.kinds.get(name, "")
                constraint = f" {kind} id * 2" if kind in {"MATERIALIZED", "ALIAS"} else ""
                if kind == "EPHEMERAL":
                    constraint = " EPHEMERAL"
                definitions.append(f"`{name}` {native}{constraint}")
            columns = ", ".join(definitions)
            rows = [(f"CREATE TABLE sandbox.events ({columns}) ENGINE=MergeTree ORDER BY tuple()",)]
        elif statement.startswith("DESCRIBE TABLE"):
            rows = [
                (name, native, self.store.kinds.get(name, ""), "", "", "", "")
                for name, native in self.store.schema.items()
            ]
        elif statement.startswith("SELECT getSetting("):
            self.store.metadata_queries.append(statement)
            rows = [(int(self.store.materialized_allowed),)]
        elif statement == "SELECT currentDatabase()":
            rows = [("sandbox",)]
        else:
            rows = []
        return SimpleNamespace(result_rows=rows)

    def insert_df(self, *, table, df, column_names):
        del table
        get_backend_adapter("ch").column_list_sql(column_names)
        self.store.insert(column_names, list(df.itertuples(index=False, name=None)))

    def insert(self, *, table, data, column_names, **_kwargs):
        del table
        get_backend_adapter("ch").column_list_sql(column_names)
        self.store.insert(column_names, data)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def existing_column_target(
    monkeypatch,
    backend,
    columns=("id",),
    *,
    real_metadata=False,
    real_gp_adaptation=False,
    **settings,
):
    store = ExistingColumnStore(backend, columns, **settings)
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda _key: ColumnConnection(store))
    # Metadata and driver I/O are fake; validation, conversion, destructive
    # preparation and each backend's real INSERT formatter remain active.
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_args, **_kwargs: True)
    if not real_metadata:
        monkeypatch.setattr(
            load_df_module, "get_table_column_types", lambda *_args, **_kwargs: dict(store.schema)
        )

    def execute_values(cursor, statement, rows, page_size, **_kwargs):
        del page_size
        if real_gp_adaptation:
            from psycopg2.extensions import (  # noqa: PLC0415 - optional real-driver boundary
                QuotedString,
                adapt,
            )

            for row in rows:
                for value in row:
                    adapted = adapt(value)
                    if isinstance(adapted, QuotedString):
                        # A standalone adapter lacks the real connection's UTF8
                        # client encoding and otherwise defaults to Latin-1.
                        adapted.encoding = "utf-8"
                    adapted.getquoted()
        placeholders = ", ".join("(" + ", ".join("NULL" for _ in row) + ")" for row in rows)
        sql = statement % placeholders
        cursor.store.execute(sql, [value for row in rows for value in row])

    monkeypatch.setattr(gp_insert_module, "execute_values", execute_values)
    return store
