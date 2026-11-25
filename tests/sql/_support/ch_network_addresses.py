"""Stateful dataframe endpoint using ClickHouse's real column serializers."""

from __future__ import annotations

import datetime as dt
from ipaddress import IPv4Address, IPv6Address
from types import SimpleNamespace

from tests.sql._support.load_column_state import ColumnConnection, existing_column_target
from tests.sql._support.load_table import load_df_module
from tests.sql.backends.ch._support.exact_transport import native_context
from tests.sql.backends.ch._support.network_transport import encode_normalized


class NetworkConnection(ColumnConnection):
    def __init__(self, store):
        super().__init__(store)
        if store.native:
            self.context = native_context()

    def query(self, statement):
        if statement == "SELECT timezone()":
            self.store.metadata_queries.append(statement)
            return SimpleNamespace(result_rows=[(self.store.timezone,)])
        return super().query(statement)

    def _store_encoded(self, column_names, rows):
        # Native INSERT VALUES obtains binary column types from the server sample
        # block; supplied type names are normalization hints only.
        types = [self.store.schema[name] for name in column_names]
        for index, (name, native_type) in enumerate(zip(column_names, types)):
            values = [row[index] for row in rows]
            wire = encode_normalized(native_type, values, self.store.transport)
            self.store.wire_columns.append((name, native_type, wire))
        self.store.bound_rows.extend(tuple(row) for row in rows)
        self.store.insert(column_names, rows)

    def insert_df(self, *, table, df, column_names):
        assert table == "sandbox.events"
        self._store_encoded(column_names, list(df.itertuples(index=False, name=None)))

    def insert(self, *, table, data, column_names, column_type_names):
        assert table == "sandbox.events"
        self.store.normalization_types.append(tuple(column_type_names))
        self._store_encoded(column_names, list(data))


def network_target(monkeypatch, transport, native_type, *, exists=True, clock_type=None):
    store = existing_column_target(
        monkeypatch,
        "ch",
        columns=("address", "clock") if clock_type is not None else ("address",),
        real_metadata=True,
        native=transport == "native",
    )
    store.transport = transport
    store.schema = {"address": native_type}
    address_type = IPv6Address if "IPv6" in native_type else IPv4Address
    seed = address_type("2001:db8::99" if address_type is IPv6Address else "198.51.100.99")
    store.rows = (
        [{"address": [seed] if native_type.startswith("Array(") else seed}] if exists else []
    )
    store.timezone = "America/New_York" if clock_type is not None else None
    if clock_type is not None:
        store.schema["clock"] = clock_type
        for row in store.rows:
            row["clock"] = dt.datetime(2024, 12, 31, 12, tzinfo=dt.timezone.utc)
    store.wire_columns = []
    store.bound_rows = []
    store.normalization_types = []
    store.create_calls = []
    create = load_df_module._create_sql_table_with_connection

    def observe_create(*args, **kwargs):
        store.create_calls.append((args, kwargs))
        return create(*args, **kwargs)

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda _key: NetworkConnection(store))
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_args, **_kwargs: exists)
    monkeypatch.setattr(load_df_module, "_create_sql_table_with_connection", observe_create)
    return store
