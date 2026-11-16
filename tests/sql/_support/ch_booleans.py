"""ClickHouse Bool targets decoded from actual installed-driver binary writes."""

from __future__ import annotations

from copy import deepcopy

from tests.sql._support.load_client_validation import _table_identity
from tests.sql._support.load_column_state import ColumnConnection, ExistingColumnStore
from tests.sql._support.load_table import load_df_module
from tests.sql.backends.ch._support.exact_transport import native_context
from tests.sql.backends.ch._support.network_transport import encode_normalized


class _BooleanConnection(ColumnConnection):
    def __init__(self, store):
        super().__init__(store)
        self.close_calls = 0
        self.closed = False
        if store.native:
            self.context = native_context()

    def outage_table_exists(self, table):
        return _table_identity(table) == ("sandbox", "events")

    def _write(self, table, names, rows):
        assert table == "sandbox.events"
        assert list(names) == ["payload"]
        rows = deepcopy([tuple(row) for row in rows])
        native = self.store.schema["payload"]
        wire = encode_normalized(native, [row[0] for row in rows], self.store.transport)
        self.store.wire_columns.append(("payload", native, wire))
        self.store.bound_rows.extend(rows)
        nullable = native.startswith("Nullable(")
        offset = len(rows) if nullable else 0
        assert len(wire) == len(rows) + offset
        decoded = [
            (None if nullable and wire[index] else wire[offset + index] != 0,)
            for index in range(len(rows))
        ]
        self.store.insert(names, decoded)

    def insert_df(self, *, table, df, column_names):
        self._write(table, column_names, df.itertuples(index=False, name=None))

    def insert(self, *, table, data, column_names, **_kwargs):
        self._write(table, column_names, data)

    def close(self):
        self.close_calls += 1
        self.closed = True


def native_boolean_target(monkeypatch, transport, native):
    """Replace metadata/driver I/O while retaining real conversion and serializers."""
    store = ExistingColumnStore("ch", ("payload",), native=transport == "native")
    store.transport = transport
    store.schema = {"payload": native}
    store.rows = [{"payload": True}]
    store.wire_columns = []
    store.bound_rows = []
    store.opened = []
    store.create_calls = []
    create = load_df_module._create_sql_table_with_connection

    def connect(_alias):
        connection = _BooleanConnection(store)
        store.opened.append(connection)
        return connection

    def observe_create(*args, **kwargs):
        store.create_calls.append((args, kwargs))
        return create(*args, **kwargs)

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    monkeypatch.setattr(load_df_module, "_create_sql_table_with_connection", observe_create)
    return store
