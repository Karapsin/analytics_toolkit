"""Stateful ClickHouse writer checkpoints using actual driver encoders."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.load_column_state import ColumnConnection, ExistingColumnStore
from tests.sql._support.load_table import load_df_module
from tests.sql.backends.ch._support.exact_transport import native_context
from tests.sql.backends.ch._support.network_transport import encode_normalized


def _table_identity(table):
    if isinstance(table, str):
        table = parse_one(f"SELECT * FROM {table}", read=ClickHouseDialect).find(exp.Table)
    assert isinstance(table, exp.Table)
    return table.db or "sandbox", table.name


class ClientStore(ExistingColumnStore):
    def __init__(self, transport, schema, *, exists):
        super().__init__("ch", tuple(schema) or ("payload",), native=transport == "native")
        self.transport = transport
        self.schema = dict(schema)
        self.rows = (
            [
                {
                    name: []
                    if native.startswith("Nested(")
                    else [(99.0, 100.0)]
                    if native == "Ring"
                    else {"a": 99}
                    if native == "Object('json')"
                    else 101
                    for name, native in schema.items()
                }
            ]
            if exists
            else []
        )
        self.settings = (
            {"flatten_nested": 0}
            if any(native.startswith("Nested(") for native in schema.values())
            else {}
        )
        self.schemas = {("sandbox", "events"): self.schema} if exists else {}
        self.table_rows = {("sandbox", "events"): self.rows} if exists else {}
        self.ddls = {}
        self.wire_columns = []
        self.bound_rows = []
        self.create_schemas = []
        self.write_generations = []
        self.type_hints = []
        self.type_probes = []
        self.generation = 0
        self.fail_generation = 1
        self.on_lost_ack = None
        self.lost_ack_notified = False

    def execute(self, statement, params=None):
        assert params is None
        tree = parse_one(statement, read=ClickHouseDialect)
        if isinstance(tree, exp.Create):
            assert isinstance(tree.this, exp.Schema)
            identity = _table_identity(tree.this.this)
            if identity in self.schemas:
                assert tree.args.get("exists")
                return
            schema = {
                column.name: column.kind.sql(dialect=ClickHouseDialect)
                for column in tree.this.expressions
                if isinstance(column, exp.ColumnDef)
            }
            assert schema
            self.schemas[identity] = schema
            self.table_rows[identity] = []
            self.ddls[identity] = statement
            self.create_schemas.append(dict(schema))
            self.submissions.append(("CREATE", statement))
            self._select_target(identity)
        elif isinstance(tree, exp.Drop):
            tables = tree.args.get("tables") or [tree.this]
            assert len(tables) == 1
            identity = _table_identity(tables[0])
            assert identity in self.schemas or tree.args.get("exists")
            self.schemas.pop(identity, None)
            self.table_rows.pop(identity, None)
            self.ddls.pop(identity, None)
            self.submissions.append(("DROP", statement))
            if identity == ("sandbox", "events"):
                self.schema = {}
                self.rows = []
        elif isinstance(tree, (exp.Delete, exp.TruncateTable)):
            identity = _table_identity(tree.find(exp.Table))
            assert identity in self.schemas
            self.table_rows[identity].clear()
            self.submissions.append(("CLEAR", statement))
        else:
            message = f"Unexpected statement at the client-validation endpoint: {statement}"
            raise TypeError(message)

    def _select_target(self, identity):
        if identity == ("sandbox", "events"):
            self.schema = self.schemas[identity]
            self.rows = self.table_rows[identity]

    def record_write(self, table, column_names, rows, column_types):
        identity = _table_identity(table)
        assert identity == ("sandbox", "events")
        assert identity in self.schemas
        assert len(column_names) == len(column_types)
        for index, (name, native) in enumerate(zip(column_names, column_types)):
            values = [row[index] for row in rows]
            encoded = encode_normalized(
                native, deepcopy(values), self.transport, settings=self.settings
            )
            self.wire_columns.append((name, native, encoded))
        self.bound_rows.extend(deepcopy([tuple(row) for row in rows]))
        self.write_generations.append(self.generation)
        self.insert(column_names, rows)
        if self.generation == self.fail_generation:
            if not self.lost_ack_notified:
                self.lost_ack_notified = True
                if self.on_lost_ack is not None:
                    self.on_lost_ack()
            message = "The write committed, but its acknowledgement was lost."
            raise ConnectionError(message)


class ClientConnection(ColumnConnection):
    def __init__(self, store):
        super().__init__(store)
        if self.is_native_transport:
            self.context = native_context()
            self.context.settings.update(store.settings)

    def outage_table_exists(self, table):
        return _table_identity(table) in self.store.schemas

    def outage_assert_new_target(self, table, **_kwargs):
        assert not self.outage_table_exists(table)

    def outage_after_create(self, table, *, policy, pair, expected_columns):
        del policy
        assert not pair
        identity = _table_identity(table)
        assert identity in self.store.schemas
        if expected_columns is not None:
            expected = {
                name: exp.DataType.build(native, dialect=ClickHouseDialect).sql(
                    dialect=ClickHouseDialect
                )
                for name, native in dict(expected_columns).items()
            }
            assert self.store.schemas[identity] == expected
        return True

    def query(self, statement):
        if statement.startswith("SELECT toTypeName(defaultValueOfTypeName("):
            tree = parse_one(statement, read=ClickHouseDialect)
            assert isinstance(tree, exp.Select)
            assert not list(tree.find_all(exp.Table))
            requested = [literal.this for literal in tree.find_all(exp.Literal)]
            assert requested == ["BIGINT"]
            self.store.type_probes.append(
                (statement, dict(self.store.schema), list(self.store.bound_rows))
            )
            rows = [("Int64",)]
        elif statement.startswith("EXISTS TABLE "):
            rows = [(int(self.outage_table_exists(statement[len("EXISTS TABLE ") :])),)]
        elif statement.startswith("DESCRIBE TABLE "):
            identity = _table_identity(statement[len("DESCRIBE TABLE ") :])
            assert identity in self.store.schemas
            rows = [
                (name, native, "", "", "", "", "")
                for name, native in self.store.schemas[identity].items()
            ]
        elif statement.startswith("SHOW CREATE TABLE "):
            identity = _table_identity(statement[len("SHOW CREATE TABLE ") :])
            assert identity in self.store.schemas
            ddl = self.store.ddls.get(identity)
            if ddl is None:
                columns = ", ".join(
                    f"`{name}` {native}" for name, native in self.store.schemas[identity].items()
                )
                ddl = f"CREATE TABLE sandbox.events ({columns}) ENGINE=MergeTree ORDER BY tuple()"
            rows = [(ddl,)]
        elif statement == "SELECT timezone()":
            rows = [("UTC",)]
        else:
            return super().query(statement)
        self.store.metadata_queries.append(statement)
        return SimpleNamespace(result_rows=rows)

    def insert_df(self, *, table, df, column_names):
        types = [self.store.schema[name] for name in column_names]
        self.store.record_write(
            table, column_names, list(df.itertuples(index=False, name=None)), types
        )

    def insert(self, *, table, data, column_names, column_type_names=None):
        self.store.type_hints.append(
            tuple(zip(column_names, column_type_names)) if column_type_names is not None else None
        )
        types = [self.store.schema[name] for name in column_names]
        self.store.record_write(table, column_names, list(data), types)


def client_target(monkeypatch, transport, schema, *, exists=True):
    store = ClientStore(transport, schema or {}, exists=exists)
    initialize = load_df_module._initialize_load_state

    def initialize_generation(*args, **kwargs):
        store.generation += 1
        return initialize(*args, **kwargs)

    def connect(_key):
        return ClientConnection(store)

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.runtime.retry.get_sql_connection", connect
    )
    monkeypatch.setattr(load_df_module, "_initialize_load_state", initialize_generation)
    monkeypatch.setattr(
        load_df_module,
        "table_exists",
        lambda _backend, connection, table, **_kwargs: connection.outage_table_exists(table),
    )
    return store
