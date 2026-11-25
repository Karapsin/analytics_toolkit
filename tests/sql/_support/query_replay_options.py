"""Frozen query replay over actual DBAPI writers and fake network sessions."""

from __future__ import annotations

from copy import deepcopy

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.core import replay_spool
from psycopg2 import InterfaceError, OperationalError, extensions
from sqlglot import exp, parse_one

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.assertions import strip_query_label
from tests.sql._support.created_gp_relations import locked_relation
from tests.sql._support.query_native_assignment import (
    QueryNativeConnection,
    QueryNativeCursor,
    QueryNativeDatabase,
)
from tests.sql._support.trino_parameters import DriverParameterConnection, DriverParameterCursor


def _connections(monkeypatch, write_sql_connections, tmp_path):
    configs = deepcopy(DEFAULT_SQL_CONNECTIONS)
    for native in configs.values():
        native["transfer_staging_schema"] = None
    configs["trino"]["insert_chunk_size"] = 4
    write_sql_connections(configs)
    # Failed live publication deliberately retains its real certified spool.
    # Keep that recovery artifact inside this test's automatically owned tree.
    monkeypatch.setattr(replay_spool.tempfile, "tempdir", str(tmp_path))


class ReplayTrinoDatabase:
    table = "iceberg.sandbox.events"

    def __init__(self, rows, native_type):
        self.rows = list(rows)
        self.native_type = native_type
        self.exists = True
        self.opened = []
        self.events = []
        self.reads = 0
        self.probes = 0
        self.insert_attempts = []
        self.failures = []
        self.always_fail = None


class ReplayTrinoConnection(DriverParameterConnection):
    def __init__(self, database):
        super().__init__()
        self.database = database

    def cursor(self):
        return ReplayTrinoCursor(self)


def _replay_literal_value(wire):
    literal = parse_one(wire, read="trino")
    assert isinstance(literal, (exp.Literal, exp.Null))
    if isinstance(literal, exp.Null):
        return None
    return literal.this if literal.is_string else int(literal.this)


class ReplayTrinoCursor(DriverParameterCursor):
    def __init__(self, connection):
        super().__init__(connection)
        self.description = None
        self.rows = []
        self.is_source = False
        self.complete = False

    def execute(self, statement, values=None):
        super().execute(statement, values)
        database = self.connection.database
        tree = parse_one(strip_query_label(statement), read="trino")
        self.rows = []
        self.description = None
        if isinstance(tree, exp.Select):
            table = tree.find(exp.Table)
            if table is not None and table.db == "information_schema":
                assert table.name == "tables"
                self.rows = [(1,)] if database.exists else []
            elif any(item.alias == "source_schema_probe" for item in tree.find_all(exp.Subquery)):
                database.probes += 1
                self.description = [("id", database.native_type, None, None, None, None, None)]
            elif tree.find(exp.Count):
                self.rows = [(len(database.rows),)]
            else:
                assert database.exists
                assert database.reads == 0, "Certified source was queried again after capture."
                database.reads += 1
                database.events.append("scan")
                self.rows = list(database.rows)
                self.description = [("id", database.native_type, None, None, None, None, None)]
                self.is_source = True
        elif isinstance(tree, exp.Drop):
            database.rows = []
            database.exists = False
            database.events.append("drop")
        elif isinstance(tree, exp.Create):
            assert isinstance(tree.this, exp.Schema)
            assert not database.exists
            database.exists = True
            database.rows = []
            database.native_type = tree.this.expressions[0].args["kind"].sql(dialect="trino")
            database.events.append("create")
        elif isinstance(tree, exp.Insert):
            assert isinstance(tree.expression, exp.Values)
            record = self.connection.statements[-1]
            database.insert_attempts.append((self.connection, *record))
            if database.always_fail is not None:
                raise database.always_fail
            if database.failures:
                raise database.failures.pop(0)
            inserted = [_replay_literal_value(wire) for wire in record[2]]
            database.rows.extend((value,) for value in inserted)
            database.events.append("insert")
            self.rowcount = len(inserted)
        else:
            message = f"Unexpected query replay network statement: {type(tree).__name__}"
            raise TypeError(message)
        return self

    def fetchmany(self, size):
        rows, self.rows = self.rows[:size], self.rows[size:]
        if self.is_source and not rows and not self.complete:
            self.connection.database.events.append("input_complete")
            self.complete = True
        return rows

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


def trino_replay_target(monkeypatch, write_sql_connections, tmp_path, *, rows, native="BIGINT"):
    _connections(monkeypatch, write_sql_connections, tmp_path)
    database = ReplayTrinoDatabase(rows, native)

    def connect(config, **_kwargs):
        client = ReplayTrinoConnection(database)
        database.opened.append(client)
        return client

    monkeypatch.setattr(get_backend_adapter("trino"), "open_connection", connect)
    return database


class ReplayGpConnection(QueryNativeConnection):
    def __init__(self, database, alias):
        super().__init__(database, alias)
        self.broken = False
        self.close_calls = 0
        self.rollback_calls = 0

    def cursor(self):
        if self.broken:
            self.database.reused_broken_sessions.append(self)
            message = "the first target session is no longer usable"
            raise InterfaceError(message)
        return ReplayGpCursor(self)

    def rollback(self):
        self.rollback_calls += 1
        super().rollback()

    def close(self):
        self.close_calls += 1
        super().close()


class ReplayGpCursor(QueryNativeCursor):
    def execute(self, statement, params=None):
        text = statement.decode("utf-8") if isinstance(statement, bytes) else statement
        if locked_relation(text) is not None:
            return super().execute(statement, params)
        database = self.connection.database
        tree = parse_one(database._ddl_schema_prefix(text), read="postgres")
        if isinstance(tree, exp.Insert) and isinstance(tree.expression, exp.Values):
            database.live_insert_sessions.append(self.connection)
            if database.fail_first_insert:
                database.fail_first_insert = False
                self.connection.broken = True
                raise database.driver_failure
        result = super().execute(statement, params)
        native_oids = {"BIGINT": 20, "INTEGER": 23, "TEXT": 25}
        self.description = [
            (column[0], native_oids.get(column[1], column[1]), *column[2:])
            for column in self.description or []
        ]
        return result


def gp_replay_target(monkeypatch, write_sql_connections, tmp_path):
    _connections(monkeypatch, write_sql_connections, tmp_path)
    database = QueryNativeDatabase([(1,), (2,), (3,)], native_type="BIGINT")
    database.fail_first_insert = True
    database.driver_failure = OperationalError("the selected target connection was lost")
    database.live_insert_sessions = []
    database.reused_broken_sessions = []

    def register_type(_caster, cursor):
        assert isinstance(cursor, QueryNativeCursor)

    def connect(config, **_kwargs):
        client = ReplayGpConnection(database, config.connection_key)
        database.opened.append(client)
        return client

    monkeypatch.setattr(extensions, "register_type", register_type)
    monkeypatch.setattr(get_backend_adapter("gp"), "open_connection", connect)
    return database
