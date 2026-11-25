"""A native SDK connection writes real binary columns over a stateful target."""

from __future__ import annotations

from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from typing import Any

from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.native_client import NativeClickHouseClient
from analytics_toolkit.sql.dml.table._basic_ops import table_exists
from sqlglot import TokenType, exp, parse_one

from tests.sql._support.load_client_validation import ClientConnection, client_target
from tests.sql._support.load_table import load_df_module
from tests.sql.backends.ch._support.exact_transport import native_context


class NativeStringEndpoint:
    def __init__(self, store: Any, *, encoding: str = "utf-8") -> None:
        # Load only the selected optional native SDK.
        from clickhouse_driver import Client  # noqa: PLC0415

        self.store = store
        self.bridge = ClientConnection(store)
        self.raw = Client("127.0.0.1", settings={"strings_encoding": encoding})
        context = native_context()
        context.client_settings = {**context.client_settings, **self.raw.client_settings}
        self.raw.connection.context = context
        self.close_calls = 0
        self.insert_attempts = 0
        self.opened = []
        self.client = NativeClickHouseClient(self.raw)
        self.raw.execute = self.execute
        self.raw.disconnect_connection = self.close

    def close(self) -> None:
        self.close_calls += 1

    @property
    def total_insert_attempts(self) -> int:
        return (
            sum(item.insert_attempts for item in self.opened)
            if self.opened
            else self.insert_attempts
        )

    def execute(self, statement: str, data: Any = None, **kwargs: Any) -> Any:
        if data is not None:
            return self._insert(statement, data, kwargs.get("settings") or {})
        if kwargs.get("with_column_types"):
            result = self.bridge.query(statement)
            rows = result.result_rows
            width = len(rows[0]) if rows else 0
            names = (
                ["name", "type", "default_type", "default_expression", "comment", "codec", "ttl"]
                if statement.startswith("DESCRIBE TABLE ")
                else [f"value{index}" for index in range(width)]
            )
            header = [(name, "String") for name in names]
            values = list(zip(*rows)) if kwargs.get("columnar") else rows
            return values, header
        return self.store.execute(statement)

    def _insert(self, statement: str, rows: Any, settings: Any) -> int:
        # Load only the selected optional native SDK.
        from clickhouse_driver.bufferedwriter import BufferedSocketWriter  # noqa: PLC0415

        # Load only the selected optional native SDK.
        from clickhouse_driver.columns.service import write_column  # noqa: PLC0415

        self.insert_attempts += 1
        tokens = ClickHouseDialect().tokenize(statement)
        assert tokens[-1].token_type == TokenType.VALUES
        # Native VALUES data travels outside the SQL header. A static null row
        # lets the structured parser inspect that header without interpreting
        # or modifying any supplied binary values.
        tree = parse_one(statement + " (NULL)", read=ClickHouseDialect)
        assert isinstance(tree, exp.Insert)
        names = [column.name for column in tree.this.expressions]
        context = native_context()
        context.client_settings = {
            **context.client_settings,
            **self.raw.client_settings,
            **settings,
        }
        encoded = []
        for index, name in enumerate(names):
            native = self.store.schema[name]
            output = BytesIO()
            writer = BufferedSocketWriter(SimpleNamespace(sendall=output.write), 256)
            write_column(context, name, native, [row[index] for row in rows], writer)
            writer.flush()
            encoded.append((name, native, output.getvalue()))
        self.store.wire_columns.extend(encoded)
        self.store.bound_rows.extend(deepcopy([tuple(row) for row in rows]))
        self.store.insert(names, rows)
        return len(rows)


def _seed_value(native: str) -> Any:
    if native.startswith("Array("):
        return ["seed"]
    if native.startswith("Tuple("):
        return ("seed",)
    if native.startswith("Map("):
        return {"seed": "seed"}
    return "seed"


def native_string_target(
    monkeypatch: Any,
    schema: dict[str, str],
    *,
    encoding: str = "utf-8",
    write_sql_connections: Any = None,
) -> Any:
    if write_sql_connections is not None:
        path = write_sql_connections(
            {
                "ch": {
                    "type": "ch",
                    "driver": "native",
                    "host": "fixture",
                    "user": "fixture",
                    "password": "",
                    "database": "sandbox",
                    "settings": {"strings_encoding": encoding},
                    "ddl_defaults": {
                        scope: {
                            "create_distributed_pair": False,
                            "shard": {"engine": "MergeTree", "on_cluster": None},
                        }
                        for scope in ("regular", "staging")
                    },
                }
            }
        )
        set_connections_path(path)
    store = client_target(monkeypatch, "native", schema)
    store.rows[:] = [{name: _seed_value(native) for name, native in schema.items()}]
    store.fail_generation = 0
    endpoint = NativeStringEndpoint(store, encoding=encoding)

    def connect(_key):
        current = NativeStringEndpoint(store, encoding=encoding) if endpoint.opened else endpoint
        endpoint.opened.append(current)
        return current.client

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.runtime.retry.get_sql_connection", connect
    )
    monkeypatch.setattr(load_df_module, "table_exists", table_exists)
    return store, endpoint
