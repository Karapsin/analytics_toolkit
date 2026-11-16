"""Authoritative integer targets encoded through installed CH SDK writers."""

from __future__ import annotations

from types import SimpleNamespace

from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.ch_integer_timestamps import _TimestampConnection, timestamp_target
from tests.sql._support.load_table import load_df_module


def integer_target(monkeypatch, transport, native_type):
    """Retain real metadata and codecs for every declared integer width."""
    return timestamp_target(monkeypatch, transport, native_type, seed=1)


def _target_identity(table):
    assert isinstance(table, exp.Table)
    assert not table.catalog
    assert (table.db or "sandbox", table.name) == ("sandbox", "events")


class _CreatingConnection(_TimestampConnection):
    def __init__(self, store):
        super().__init__(store)
        self._ch_outage_client = False

    def command(self, statement, **kwargs):
        tree = parse_one(statement, read=ClickHouseDialect)
        if not isinstance(tree, exp.Create):
            return super().command(statement, **kwargs)
        assert isinstance(tree.this, exp.Schema)
        _target_identity(tree.this.this)
        assert not self.store.exists
        definitions = tree.this.expressions
        assert definitions
        assert all(isinstance(column, exp.ColumnDef) for column in definitions)
        self.store.schema = {
            column.name: column.args["kind"].sql(dialect=ClickHouseDialect)
            for column in definitions
        }
        self.store.rows = []
        self.store.exists = True
        self.store.created_sql.append(statement)
        self.store.submissions.append(("CREATE", statement))
        return None

    def query(self, statement):
        for prefix in ("EXISTS TABLE ", "SHOW CREATE TABLE ", "DESCRIBE TABLE "):
            if statement.startswith(prefix):
                table = parse_one(statement[len(prefix) :], read=ClickHouseDialect, into=exp.Table)
                _target_identity(table)
                if prefix == "EXISTS TABLE ":
                    self.store.metadata_queries.append(statement)
                    return SimpleNamespace(result_rows=[(int(self.store.exists),)])
                assert self.store.exists
                if prefix == "SHOW CREATE TABLE ":
                    self.store.metadata_queries.append(statement)
                    return SimpleNamespace(result_rows=[(self.store.created_sql[-1],)])
        return super().query(statement)


def inferred_string_target(monkeypatch, write_sql_connections, transport):
    """Create a local table from real inferred DDL, then use installed String codecs."""
    scope = {"create_distributed_pair": False, "shard": {"engine": "Memory", "on_cluster": None}}
    path = write_sql_connections(
        {
            "ch": {
                "type": "ch",
                "driver": transport,
                "host": "clickhouse.example",
                "port": 9000 if transport == "native" else 8123,
                "user": "user",
                "password": "password",
                "database": "sandbox",
                "ddl_defaults": {"regular": scope, "staging": scope},
            }
        }
    )
    set_connections_path(path)
    store = timestamp_target(monkeypatch, transport, "String", seed=None)
    store.schema = {}
    store.rows = []
    store.exists = False
    store.created_sql = []

    def connect(_alias):
        connection = _CreatingConnection(store)
        store.opened.append(connection)
        return connection

    def exists(_backend, _connection, table_name, **_kwargs):
        _target_identity(parse_one(table_name, read=ClickHouseDialect, into=exp.Table))
        return store.exists

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    monkeypatch.setattr(load_df_module, "table_exists", exists)
    return store
