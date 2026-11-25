"""Existing timestamp targets encoded through the toolkit's exact wire codecs."""

from __future__ import annotations

from copy import deepcopy
from functools import wraps
from io import BytesIO
from types import SimpleNamespace

from analytics_toolkit.sql.backends.ch.http_codec import exact_http_type
from analytics_toolkit.sql.backends.ch.native_codec import _prepare_column

from tests.sql._support.load_column_state import ColumnConnection, ExistingColumnStore
from tests.sql._support.load_table import load_df_module
from tests.sql.backends.ch._support.exact_transport import native_context


def _encode_http(name, spec, values, settings):
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415
    from clickhouse_connect.driver.insert import InsertContext  # noqa: PLC0415

    datatype = exact_http_type(get_from_name(spec), default_timezone="UTC")
    context = InsertContext("sandbox.events", [name], [datatype], settings=settings)
    context.start_column(name)
    output = bytearray()
    datatype.write_column(deepcopy(values), output, context)
    return bytes(output)


def _encode_native(_name, spec, values, settings):
    from clickhouse_driver.bufferedwriter import BufferedSocketWriter  # noqa: PLC0415
    from clickhouse_driver.columns.service import get_column_by_spec  # noqa: PLC0415

    context = native_context()
    context.settings.update(settings)
    column = _prepare_column(
        get_column_by_spec(spec, {"context": context, "types_check": False}, use_numpy=False)
    )
    output = BytesIO()
    writer = BufferedSocketWriter(SimpleNamespace(sendall=output.write), 256)
    items = deepcopy(values)
    column.write_state_prefix(writer, items)
    column.write_data(items, writer)
    writer.flush()
    return output.getvalue()


class _TimestampConnection(ColumnConnection):
    server_tz = "UTC"
    effective_read_timezone = "UTC"
    effective_write_timezone = "UTC"

    def __init__(self, store):
        super().__init__(store)
        self.close_calls = 0
        if self.is_native_transport:
            self.context = native_context()
            self.context.settings.update(store.settings)

    def query(self, statement):
        result = super().query(statement)
        if not statement.startswith("SELECT getSetting("):
            self.store.metadata_queries.append(statement)
        return result

    def _write(self, names, rows):
        rows = deepcopy([tuple(row) for row in rows])
        encoder = _encode_native if self.is_native_transport else _encode_http
        encoded = [
            (
                name,
                self.store.schema[name],
                encoder(
                    name,
                    self.store.schema[name],
                    [row[index] for row in rows],
                    self.store.settings,
                ),
            )
            for index, name in enumerate(names)
        ]
        self.store.wire_columns.extend(encoded)
        self.store.bound_rows.extend(rows)
        self.store.insert(names, rows)

    def insert_df(self, *, table, df, column_names):
        assert table == "sandbox.events"
        self._write(column_names, df.itertuples(index=False, name=None))

    def insert(self, *, table, data, column_names, **_kwargs):
        assert table == "sandbox.events"
        self._write(column_names, data)

    def close(self):
        self.close_calls += 1


def timestamp_target(monkeypatch, transport, native_type, seed=101):
    store = ExistingColumnStore("ch", ("payload",), native=transport == "native")
    store.schema = {"payload": native_type}
    store.rows = [{"payload": deepcopy(seed)}]
    store.settings = {}
    store.wire_columns = []
    store.bound_rows = []
    store.create_calls = []
    store.opened = []

    def connect(_alias):
        connection = _TimestampConnection(store)
        store.opened.append(connection)
        return connection

    create = load_df_module._create_sql_table_with_connection

    @wraps(create)
    def observe_create(*args, **kwargs):
        store.create_calls.append((args, kwargs))
        return create(*args, **kwargs)

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(load_df_module, "_create_sql_table_with_connection", observe_create)
    return store
