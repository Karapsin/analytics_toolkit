"""Raw server-text sources using actual psycopg2 JSON typecaster callbacks."""

from __future__ import annotations

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.source_schema import source_column_from_description
from analytics_toolkit.sql.dml.transfer.io.source import _iter_dbapi_batches
from psycopg2 import _json, extensions

from tests.sql._support.adaptive_transfer import Connection, Scenario, attempt, install


class SourceCursor:
    def __init__(self, connection):
        self.connection = connection
        self.casters = {}
        self.index = 0
        self.closed = False
        self.description = [
            ("id", connection.oid, None, None, connection.precision, connection.scale, None)
        ]

    def execute(self, query):
        self.connection.queries.append(query)

    def fetchmany(self, size):
        documents = self.connection.documents[self.index : self.index + size]
        self.index += len(documents)
        if self.connection.oid == 869 and self.connection.oid not in self.casters:
            # psycopg2's default INET scalar transport is text; there is no
            # opt-in global register_ipaddress call in the source connection.
            rows = [(document,) for document in documents]
            self.connection.decoded.extend(rows)
            return rows
        default = _json._create_json_typecasters(self.connection.oid, None)[0]
        caster = self.casters.get(self.connection.oid, default)
        # Calling psycopg2's C typecaster object without a genuine libpq cursor
        # is unsafe. Exercise the actual registered Python decoder callback,
        # captured from the driver's new_type factory, instead of its C bridge.
        callback = self.connection.callbacks[id(caster)]
        rows = [(callback(document, None),) for document in documents]
        self.connection.decoded.extend(rows)
        return rows

    def close(self):
        self.closed = True


class SourceConnection(Connection):
    def __init__(self, oid, documents, precision=None, scale=None):
        self.oid = oid
        self.precision = precision
        self.scale = scale
        self.documents = list(documents)
        self.queries = []
        self.cursors = []
        self.closed = False
        self.callbacks = {}
        self.decoded = []

    def cursor(self):
        cursor = SourceCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True


def install_source_transfer(  # noqa: PLR0913 -- explicit physical type and typmod fixture inputs.
    monkeypatch, native, oid, documents, *, frozen, precision=None, scale=None
):
    if frozen and native == "JSONB":
        oid = 114  # A truthful JSON source requires validation before JSONB assignment.
    connection = SourceConnection(oid, documents, precision, scale)
    original_new_type = extensions.new_type

    def new_type(oids, name, callback):
        caster = original_new_type(oids, name, callback)
        connection.callbacks[id(caster)] = callback
        return caster

    def register(caster, scope=None):
        assert isinstance(scope, SourceCursor), "Exact source codecs must be cursor-local"
        for registered_oid in caster.values:
            scope.casters[registered_oid] = caster

    def read(scenario, key, backend, ref, query, *_args, **_kwargs):
        scenario.read_count += 1
        scenario.events.append(f"read:{query}")
        yield from _iter_dbapi_batches(key, backend, ref, query, lambda: 1, 1, 0)
        scenario.events.append(f"complete:{query}")

    monkeypatch.setattr(extensions, "register_type", register)
    monkeypatch.setattr(_json, "register_type", register)
    monkeypatch.setattr(extensions, "new_type", new_type)
    monkeypatch.setattr(_json, "new_type", new_type)
    monkeypatch.setattr(Scenario, "read", read)
    scenario = install(
        monkeypatch,
        native_type=native,
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        table_schema={"id": native},
    )
    original_connect = attempt.get_sql_connection

    def connect(alias):
        if alias == "gp":
            scenario.source_open_count += 1
            return connection
        return original_connect(alias)

    monkeypatch.setattr(attempt, "get_sql_connection", connect)
    scenario.source_schema[0] = source_column_from_description(
        ("id", connection.oid, None, None, precision, scale, None),
        type_code_name=get_backend_adapter("gp").type_code_name,
    )
    return scenario, connection
