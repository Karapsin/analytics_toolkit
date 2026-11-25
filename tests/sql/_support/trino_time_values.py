"""Native Trino clock parameters rendered by the installed SDK without HTTP."""

from __future__ import annotations

import datetime as dt
import traceback
from types import SimpleNamespace

from analytics_toolkit.sql.backends.type_resolution import parse_native_type
from sqlglot import exp, parse_one

from tests.sql._support.fakes import FakeDbapiConnection
from tests.sql._support.load_table import load_df_module
from tests.sql._support.trino_clock_engine import inserted_clock_values, is_clock_type
from tests.sql._support.trino_parameters import DriverParameterConnection, DriverParameterCursor


class NativeTimeDatabase:
    target = ("iceberg", "sandbox", "target")

    def __init__(self, native_type, *, exists=True):
        self.native_type = native_type
        self.statements = []
        self.metadata_statements = []
        self.opened = []
        self.table_rows = {self.target: [dt.time(1), dt.time(2)]} if exists else {}
        self.table_types = {self.target: native_type} if exists else {}
        self.mutations = []
        self.failures = []
        self.lost_insert_acknowledgements = 0

    @property
    def exists(self):
        return self.target in self.table_rows

    @property
    def rows(self):
        return self.table_rows.get(self.target, [])

    @staticmethod
    def table_key(node):
        if isinstance(node, exp.Schema):
            node = node.this
        return node.catalog, node.db, node.name

    def decode_cast(self, wire):
        """Model Trino's documented native clock cast over the actual SDK wire."""
        node = parse_one(wire, read="trino")
        while isinstance(node, exp.Cast):
            node = node.this
        if isinstance(node, exp.Null):
            return None
        assert isinstance(node, exp.Literal)
        assert node.is_string
        if self.native_type.upper() in {"VARCHAR", "TEXT"}:
            return node.this
        value = dt.time.fromisoformat(node.this.replace(" ", ""))
        parsed = parse_native_type(self.native_type).base
        precision = int(parsed.arguments[0]) if parsed.arguments else 3
        micros = ((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000
        micros += value.microsecond
        if precision < 6:
            factor = 10 ** (6 - precision)
            micros = ((micros + factor // 2) // factor * factor) % 86_400_000_000
        seconds, microsecond = divmod(micros, 1_000_000)
        hour, seconds = divmod(seconds, 3600)
        minute, second = divmod(seconds, 60)
        timezone = value.tzinfo if "WITH TIME ZONE" in self.native_type.upper() else None
        return dt.time(hour, minute, second, microsecond, tzinfo=timezone)


class NativeTimeConnection(DriverParameterConnection):
    def __init__(self, database):
        super().__init__()
        self.database = database
        self.statements = database.statements
        self.metadata_statements = database.metadata_statements

    def cursor(self):
        return NativeTimeCursor(self)


class NativeTimeCursor(DriverParameterCursor):
    def execute(self, statement, values=None):
        try:
            return self._execute(statement, values)
        except AttributeError:
            self.connection.database.failures.append(traceback.format_exc())
            raise

    def _execute(self, statement, values=None):
        super().execute(statement, values)
        tree = parse_one(statement, read="trino")
        database = self.connection.database
        self.result_rows = []
        if isinstance(tree, exp.Insert):
            self._insert(tree)
        elif isinstance(tree, exp.Delete):
            self._clear([tree.this])
        elif isinstance(tree, exp.TruncateTable):
            self._clear(tree.expressions)
        elif isinstance(tree, exp.Create):
            assert isinstance(tree.this, exp.Schema)
            identity = database.table_key(tree.this)
            native_type = tree.this.expressions[0].args["kind"].sql(dialect="trino")
            database.table_rows[identity] = []
            database.table_types[identity] = native_type
            if identity == database.target:
                database.native_type = native_type
            database.mutations.append("create" if identity == database.target else "create_aux")
        elif isinstance(tree, exp.Drop):
            self._drop(tree.args.get("tables") or [tree.this])
        elif isinstance(tree, exp.Alter):
            source = database.table_key(tree.this)
            destination = database.table_key(tree.args["actions"][0].this)
            database.table_rows[destination] = database.table_rows.pop(source)
            database.table_types[destination] = database.table_types.pop(source)
            if destination == database.target:
                database.native_type = database.table_types[destination]
            database.mutations.append("rename")
        elif isinstance(tree, exp.Select):
            self._select(tree, values)
        return self

    def _clear(self, tables):
        database = self.connection.database
        for table in tables:
            identity = database.table_key(table)
            database.table_rows[identity].clear()
            database.mutations.append("clear" if identity == database.target else "clear_aux")

    def _drop(self, tables):
        database = self.connection.database
        for table in tables:
            identity = database.table_key(table)
            database.table_rows.pop(identity, None)
            database.table_types.pop(identity, None)
            database.mutations.append("drop" if identity == database.target else "drop_aux")

    def _insert(self, tree):
        database = self.connection.database
        identity = database.table_key(tree.this)
        if is_clock_type(database.table_types[identity]):
            inserted = inserted_clock_values(
                database, identity, tree, self.connection.statements[-1][2]
            )
        elif isinstance(tree.expression, exp.Select):
            source = database.table_key(tree.expression.args["from_"].this)
            inserted = list(database.table_rows[source])
        else:
            wire = self.connection.statements[-1][2]
            inserted = [database.decode_cast(value) for value in wire]
        database.table_rows[identity].extend(inserted)
        database.mutations.append("insert" if identity == database.target else "insert_aux")
        if identity == database.target and database.lost_insert_acknowledgements:
            database.lost_insert_acknowledgements -= 1
            message = "Lost acknowledgement after the native clock insert completed."
            raise ConnectionError(message)

    def _select(self, tree, values):
        database = self.connection.database
        table = tree.find(exp.Table)
        if table is not None and table.db == "information_schema" and table.name == "tables":
            identity = (table.catalog, values[0], values[1])
            self.result_rows = [(1,)] if identity in database.table_rows else []
        elif table is not None and tree.find(exp.Count):
            identity = database.table_key(table)
            self.result_rows = [(len(database.table_rows[identity]),)]

    def fetchone(self):
        return self.result_rows[0] if self.result_rows else None

    def fetchall(self):
        return self.result_rows


def native_time_target(monkeypatch, native_type, *, exists=True):
    """Patch only target acquisition/metadata; retain the real insertion adapter."""
    database = NativeTimeDatabase(native_type, exists=exists)

    def open_connection(_alias):
        client = NativeTimeConnection(database)
        database.opened.append(client)
        return client

    monkeypatch.setattr(load_df_module, "get_sql_connection", open_connection)
    monkeypatch.setattr(
        load_df_module,
        "table_exists",
        lambda _backend, _connection, name, **_options: (
            database.table_key(parse_one(name, into=exp.Table, read="trino")) in database.table_rows
        ),
    )
    monkeypatch.setattr(
        load_df_module,
        "get_table_column_types",
        lambda *_args, **_kwargs: {"payload": database.native_type},
    )
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *_args, **_kwargs: None)
    return database


def forbid_parquet_access(monkeypatch):
    calls = []

    def unexpected(*_args, **_kwargs):
        calls.append(True)
        message = "An empty dataframe must not acquire a Parquet/S3 stage."
        raise AssertionError(message)

    for name in (
        "ensure_parquet_staging_dependencies",
        "build_stage_external_location",
        "write_dataframe_to_parquet_stage",
        "cleanup_parquet_stage_location",
    ):
        monkeypatch.setattr(load_df_module, name, unexpected)
    return calls


def parquet_subset_target(monkeypatch):
    database = SimpleNamespace(commands=[], writes=[], cleaned=[], opened=[])

    def open_connection(_alias):
        client = FakeDbapiConnection()
        client.executed = database.commands
        database.opened.append(client)
        return client

    def write_frame(frame, **options):
        database.writes.append((list(frame.columns), dict(options["column_types"])))
        return len(frame)

    monkeypatch.setattr(load_df_module, "get_sql_connection", open_connection)
    monkeypatch.setattr(
        load_df_module,
        "table_exists",
        lambda _backend, _connection, name, **_options: name == "iceberg.sandbox.target",
    )
    monkeypatch.setattr(
        load_df_module,
        "get_table_column_types",
        lambda *_args, **_kwargs: {"id": "BIGINT", "omitted_clock": "TIME(6)"},
    )
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        load_df_module, "ensure_parquet_staging_dependencies", lambda: (None, None, None)
    )
    monkeypatch.setattr(load_df_module, "write_dataframe_to_parquet_stage", write_frame)
    monkeypatch.setattr(load_df_module, "cleanup_parquet_stage_location", database.cleaned.append)
    return database
