"""Native numeric dataframe storage decoded from actual ClickHouse encoder bytes."""

from __future__ import annotations

import struct
from copy import deepcopy
from decimal import Decimal

from tests.sql._support.load_client_validation import _table_identity
from tests.sql._support.load_column_state import ColumnConnection, ExistingColumnStore
from tests.sql._support.load_table import load_df_module
from tests.sql.backends.ch._support.exact_transport import native_context
from tests.sql.backends.ch._support.network_transport import encode_normalized


def _decoded_values(native, wire, count):
    assert native.startswith("Nullable(")
    mask, body = wire[:count], wire[count:]
    if "Float32" in native:
        values = struct.unpack(f"<{count}f", body)
    elif "Float64" in native:
        values = struct.unpack(f"<{count}d", body)
    else:
        assert native == "Nullable(Decimal(38,9))"
        assert len(body) == count * 16
        coefficients = [
            int.from_bytes(body[index * 16 : (index + 1) * 16], "little", signed=True)
            for index in range(count)
        ]
        values = [
            Decimal((int(value < 0), tuple(int(digit) for digit in str(abs(value))), -9))
            for value in coefficients
        ]
    return [None if mask[index] else value for index, value in enumerate(values)]


class _NumericConnection(ColumnConnection):
    def __init__(self, store):
        super().__init__(store)
        self.closed = False
        self.close_calls = 0
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
        decoded = _decoded_values(native, wire, len(rows))
        self.store.insert(names, [(value,) for value in decoded])

    def insert_df(self, *, table, df, column_names):
        self._write(table, column_names, df.itertuples(index=False, name=None))

    def insert(self, *, table, data, column_names, **_kwargs):
        self._write(table, column_names, data)

    def close(self):
        self.closed = True
        self.close_calls += 1


def native_numeric_target(monkeypatch, transport, native):
    store = ExistingColumnStore("ch", ("payload",), native=transport == "native")
    store.transport = transport
    store.schema = {"payload": native}
    store.rows = [{"payload": Decimal("99.123456789") if "Decimal" in native else 99.5}]
    store.wire_columns = []
    store.bound_rows = []
    store.create_calls = []
    store.opened = []
    create = load_df_module._create_sql_table_with_connection

    def connect(_alias):
        connection = _NumericConnection(store)
        store.opened.append(connection)
        return connection

    def observe_create(*args, **kwargs):
        store.create_calls.append((args, kwargs))
        return create(*args, **kwargs)

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    monkeypatch.setattr(load_df_module, "_create_sql_table_with_connection", observe_create)
    return store
