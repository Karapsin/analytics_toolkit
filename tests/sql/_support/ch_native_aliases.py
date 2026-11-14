"""Public dataframe endpoints backed by installed ClickHouse type factories."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from tests.sql._support.load_column_state import ColumnConnection, existing_column_target
from tests.sql._support.load_table import load_df_module
from tests.sql.backends.ch._support.exact_transport import native_context
from tests.sql.backends.ch._support.network_transport import encode_normalized


def http_factory_available(native_type):
    """Inspect the selected SDK rather than infer support from its version."""
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415
    from clickhouse_connect.driver.exceptions import InternalError  # noqa: PLC0415

    try:
        get_from_name(native_type)
    except InternalError as error:
        if "Unrecognized ClickHouse type" not in str(error):
            raise
        return False
    return True


class AliasConnection(ColumnConnection):
    def __init__(self, store):
        super().__init__(store)
        if store.native:
            self.context = native_context()
            self.context.settings.update(store.settings)

    def query(self, statement):
        if statement == "SELECT getSetting('flatten_nested')":
            self.store.metadata_queries.append(statement)
            return SimpleNamespace(result_rows=[(self.store.settings.get("flatten_nested", 1),)])
        return super().query(statement)

    def _store_encoded(self, column_names, rows, column_type_names=None):
        types = column_type_names or [self.store.schema[name] for name in column_names]
        assert len(types) == len(column_names)
        for index, (name, native_type) in enumerate(zip(column_names, types)):
            values = [row[index] for row in rows]
            wire = encode_normalized(
                native_type,
                deepcopy(values),
                self.store.transport,
                settings=self.store.settings,
            )
            self.store.wire_columns.append((name, native_type, wire))
        self.store.bound_rows.extend(deepcopy([tuple(row) for row in rows]))
        self.store.insert(column_names, rows)

    def insert_df(self, *, table, df, column_names):
        assert table == "sandbox.events"
        self._store_encoded(column_names, list(df.itertuples(index=False, name=None)))

    def insert(self, *, table, data, column_names, column_type_names):
        assert table == "sandbox.events"
        self._store_encoded(column_names, list(data), column_type_names)


def alias_target(monkeypatch, transport, native_type, seed):
    store = existing_column_target(
        monkeypatch,
        "ch",
        columns=("payload",),
        real_metadata=True,
        native=transport == "native",
    )
    store.transport = transport
    store.schema = {"payload": native_type}
    store.rows = [{"payload": deepcopy(seed)}]
    # A real unflattened Nested column has this raw DESCRIBE type. The setting
    # documents that server representation; driver factories consume it directly.
    store.settings = {"flatten_nested": 0} if native_type.startswith("Nested(") else {}
    store.wire_columns = []
    store.bound_rows = []
    store.create_calls = []
    create = load_df_module._create_sql_table_with_connection

    def observe_create(*args, **kwargs):
        store.create_calls.append((args, kwargs))
        return create(*args, **kwargs)

    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda _key: AliasConnection(store))
    monkeypatch.setattr(load_df_module, "_create_sql_table_with_connection", observe_create)
    return store
