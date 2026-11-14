"""Query creation clients with real selected ClickHouse SDK encoders."""

from __future__ import annotations

import importlib
import json
from copy import deepcopy
from types import SimpleNamespace

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch import client_validation
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.configuration_guards import check_connection_configuration
from analytics_toolkit.sql.dml import query_input
from analytics_toolkit.sql.dml.transfer.flow import adaptive_input
from analytics_toolkit.sql.dml.transfer.runtime import retry as transfer_retry
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch
from sqlglot import exp, parse_one

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql.backends.ch._support.exact_transport import native_context
from tests.sql.backends.ch._support.network_transport import encode_normalized

create = importlib.import_module("analytics_toolkit.sql.dml.table.create_table_from_sql")
legacy = importlib.import_module("analytics_toolkit.sql.backends.ch.create_table_as")


class QueryCodecClient:
    def __init__(self, transport, native_type, values, *, exists):
        self.is_native_transport = transport == "native"
        self.transport = transport
        self.native_type = native_type
        self.physical_type = native_type
        self.normalization_hints = []
        self.destination_type_probes = []
        self.values = deepcopy(values)
        self.rows = [{"payload": deepcopy(value)} for value in values] if exists else []
        self.seed = deepcopy(self.rows)
        self.exists = exists
        self.commands = []
        self.events = []
        self.bound_rows = []
        self.wire_columns = []
        self.scans = []
        self.opens = []
        self.close_calls = 0
        self.fail_insert_ack = 0
        self.insert_attempts = 0
        self.first_insert_factories = None
        self.on_lost_ack = None
        self.settings = {"flatten_nested": 0} if native_type.startswith("Nested(") else {}
        if self.is_native_transport:
            self.context = native_context()
            self.context.settings.update(self.settings)

    def query(self, statement, **_kwargs):
        if statement.startswith("SELECT toTypeName(defaultValueOfTypeName("):
            tree = parse_one(statement, read=ClickHouseDialect)
            assert isinstance(tree, exp.Select)
            assert tree.find(exp.Table) is None
            declarations = [literal.this for literal in tree.find_all(exp.Literal)]
            self.destination_type_probes.append(declarations)
            self.events.append("destination_type_probe")
            rows = [tuple("Int64" if raw == "BIGINT" else raw for raw in declarations)]
        elif statement.startswith("EXISTS TABLE"):
            rows = [(int(self.exists),)]
        elif statement == "SELECT currentDatabase()":
            rows = [("analytics",)]
        elif statement.startswith("SHOW CREATE TABLE"):
            rows = [
                (
                    f"CREATE TABLE analytics.events (`payload` {self.native_type}) "
                    "ENGINE=MergeTree ORDER BY tuple()",
                )
            ]
        elif statement.startswith("SELECT getSetting('flatten_nested')"):
            rows = [(self.settings.get("flatten_nested", 1),)]
        elif statement.startswith("DESCRIBE TABLE"):
            rows = [("payload", self.physical_type, "", "", "", "", "")]
        else:
            rows = []
        return SimpleNamespace(result_rows=rows)

    def command(self, statement, **_kwargs):
        self.commands.append(statement)
        if statement.startswith("DROP TABLE"):
            self.events.append("drop")
            self.exists = False
            self.rows.clear()
        elif statement.startswith("CREATE TABLE"):
            tree = parse_one(statement, read=ClickHouseDialect)
            assert isinstance(tree.this, exp.Schema)
            columns = [node for node in tree.this.expressions if isinstance(node, exp.ColumnDef)]
            assert len(columns) == 1
            self.physical_type = columns[0].kind.sql(dialect=ClickHouseDialect)
            self.events.append("create")
            self.exists = True
        elif statement.startswith("INSERT INTO"):
            self.events.append("server_copy")
            self.rows.extend({"payload": deepcopy(value)} for value in self.values)

    def insert(self, *, table, data, column_names, column_type_names):
        assert table == "analytics.events"
        assert column_names == ["payload"]
        self.normalization_hints.append(tuple(column_type_names))
        actual_type = self.physical_type if self.is_native_transport else column_type_names[0]
        rows = deepcopy(list(data))
        self.insert_attempts += 1
        self.events.append("insert")
        if self.first_insert_factories is None:
            self.first_insert_factories = self.events.count("client_factory")
        wire = encode_normalized(
            actual_type,
            [row[0] for row in rows],
            self.transport,
            settings=self.settings,
        )
        self.wire_columns.append((actual_type, wire))
        self.bound_rows.extend(rows)
        if actual_type == "Object('json')":
            assert all(isinstance(row[0], str) for row in rows)
            self.rows.extend({"payload": json.loads(row[0])} for row in rows)
        else:
            self.rows.extend({"payload": deepcopy(row[0])} for row in rows)
        if self.fail_insert_ack:
            self.fail_insert_ack -= 1
            self.values = [{"poisoned after certification": True}]
            if self.on_lost_ack is not None:
                self.on_lost_ack()
            message = "Lost acknowledgement after ClickHouse query data publication."
            raise ConnectionError(message)

    def close(self):
        self.close_calls += 1


def install_query_client(  # noqa: PLR0913 -- select transport, raw type, input and existing-target state.
    monkeypatch, write_sql_connections, transport, native_type, values, *, exists=True
):
    write_sql_connections(
        {
            **DEFAULT_SQL_CONNECTIONS,
            "ch": {
                **DEFAULT_SQL_CONNECTIONS["ch"],
                "driver": transport,
                "database": "analytics",
                "transfer_staging_schema": None,
                "ddl_defaults": {
                    "regular": {
                        "create_distributed_pair": False,
                        "shard": {"engine": "MergeTree", "on_cluster": None},
                    },
                    "staging": {
                        "create_distributed_pair": False,
                        "shard": {"engine": "MergeTree", "on_cluster": None},
                    },
                },
            },
        }
    )
    client = QueryCodecClient(transport, native_type, values, exists=exists)

    def connect(alias):
        config = get_connection_config(alias)
        check_connection_configuration(config)
        client.opens.append((alias, config.driver, config.host))
        return client

    def inspect(*_args):
        return [SourceColumn("payload", native_type)]

    def read(*_args, **_kwargs):
        client.scans.append(True)
        client.events.append("scan")
        for value in deepcopy(client.values):
            yield RowBatch(["payload"], [(value,)])
        client.events.append("input_complete")

    for module in (create, legacy, adaptive_input, transfer_retry):
        monkeypatch.setattr(module, "get_sql_connection", connect)
    monkeypatch.setattr(query_input, "inspect_source_query_schema", inspect)
    monkeypatch.setattr(adaptive_input, "iter_source_batches", read)
    monkeypatch.setattr(get_backend_adapter("ch"), "after_create_table", lambda *_a, **_kw: None)
    return client


def observe_client_factories(monkeypatch, client, *, forbid=False):
    for name in ("_native_column", "_http_column"):
        original = getattr(client_validation, name)

        def factory(*args, _original=original, **kwargs):
            client.events.append("client_factory")
            assert not forbid, "Server-side SQL unexpectedly constructed a client encoder."
            return _original(*args, **kwargs)

        monkeypatch.setattr(client_validation, name, factory)


def alias_values(native_type, transport):
    if native_type.startswith("SimpleAggregateFunction("):
        return [7, 2**64 - 1]
    if native_type == "Point":
        return [(1.25, 2.5), (-3.5, 4.75)]
    if native_type.startswith("Nested("):
        return (
            [[(7, "a"), (2**64 - 1, "b")], []]
            if transport == "native"
            else [[{"x": 7, "y": "a"}, {"x": 2**64 - 1, "y": "b"}], []]
        )
    return [{"a": 1}, {"a": 2, "b": "value"}]
