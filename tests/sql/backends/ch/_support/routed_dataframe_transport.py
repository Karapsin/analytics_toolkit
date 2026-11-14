from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

from analytics_toolkit.sql.backends.ch.http_codec import ExactHTTPClient
from analytics_toolkit.sql.backends.ch.routing import ChClusterRouting, ClusterRoutingClient

from tests.sql.backends.ch._support.exact_transport import http_query_context, http_source


class HTTPInsertEndpoint:
    """Fake only network responses, retaining the installed SDK's insert pipeline."""

    def __init__(self, schema: dict[str, str]) -> None:
        from clickhouse_connect.driver.transform import NativeTransform  # noqa: PLC0415

        self.schema = schema
        self.server_tz = dt.timezone.utc
        self._transform = NativeTransform()
        self.context_tables: list[str] = []
        self.contexts: list[Any] = []
        self.payloads: list[bytes] = []
        self.queries: list[str] = []

    def query(self, sql: str, **_kwargs: Any) -> Any:
        self.queries.append(sql)
        if sql.startswith("DESCRIBE TABLE "):
            definitions = [
                {
                    "name": name,
                    "type": native,
                    "default_type": "",
                    "default_expression": "",
                    "comment": "",
                    "codec_expression": "",
                    "ttl_expression": "",
                }
                for name, native in self.schema.items()
            ]
            return SimpleNamespace(
                result_rows=[tuple(row.values()) for row in definitions],
                named_results=lambda: iter(definitions),
            )
        columns = ", ".join(f"`{name}` {native}" for name, native in self.schema.items())
        definition = f"CREATE TABLE db.events ({columns}) ENGINE=MergeTree ORDER BY tuple()"
        if "SELECT engine, engine_full, create_table_query" in sql:
            rows = [("MergeTree", "MergeTree()", definition)]
        elif "SELECT shard_num, replica_num, host_name" in sql:
            rows = [(1, 1, "host1")]
        elif "system, tables" in sql:
            rows = [("host1", "MergeTree", "MergeTree()", definition, "uuid1")]
        else:
            message = "Unexpected metadata request in the routed HTTP insert fixture."
            raise AssertionError(message)
        return SimpleNamespace(result_rows=rows)

    def _execute_operation(self, operation: Any) -> Any:
        from clickhouse_connect.driver.client import Client  # noqa: PLC0415

        return Client._execute_operation(self, operation)

    def create_query_context(self, **kwargs: Any) -> Any:
        return http_query_context(**kwargs)

    def _query_with_context(self, context: Any) -> Any:
        return self.query(context.query, settings=context.settings)

    def create_insert_context(self, *args: Any, **kwargs: Any) -> Any:
        from clickhouse_connect.driver.client import Client  # noqa: PLC0415

        table = args[0] if args else kwargs["table"]
        self.context_tables.append(table)
        return Client.create_insert_context(self, *args, **kwargs)

    def insert(self, *args: Any, **kwargs: Any) -> Any:
        from clickhouse_connect.driver.client import Client  # noqa: PLC0415

        return Client.insert(self, *args, **kwargs)

    def data_insert(self, context: Any) -> Any:
        self.contexts.append(context)
        payload = b"".join(self._transform.build_insert(context))
        if context.insert_exception is not None:
            raise context.insert_exception
        self.payloads.append(payload)
        return SimpleNamespace(written_rows=context.row_count)


def publish_dataframe(
    schema: dict[str, str], frame: Any, *, routed: bool, resolve_types: bool = False
) -> HTTPInsertEndpoint:
    raw = HTTPInsertEndpoint(schema)
    exact = ExactHTTPClient(raw)
    client = (
        ClusterRoutingClient(
            exact,
            routing=ChClusterRouting("core", "rand()"),
            database="db",
        )
        if routed
        else exact
    )
    kwargs = {} if resolve_types else {"column_type_names": list(schema.values())}
    client.insert_df("db.events", frame, column_names=list(schema), **kwargs)
    assert len(raw.payloads) == 1
    assert raw.context_tables == ["db.events"]
    prefix = raw.payloads[0].partition(b"\n")[0]
    expected = (
        b"INSERT INTO FUNCTION cluster('core', 'db', 'events', rand())"
        if routed
        else b"INSERT INTO db.events"
    )
    assert prefix.startswith(expected)
    return raw


def decoded_columns(endpoint: HTTPInsertEndpoint, *, bytes_column: str | None = None) -> list:
    payload = endpoint.payloads[0].partition(b"\n")[2]
    context = http_query_context(
        use_numpy=False,
        use_none=True,
        column_oriented=True,
        column_formats={bytes_column: "bytes"} if bytes_column is not None else None,
    )
    result = endpoint._transform.parse_response(http_source(payload), context)
    return [list(column) for column in result.result_columns]
