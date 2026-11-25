"""A lazy HTTP source executes its actual Native decoder under the client limit."""

from __future__ import annotations

from io import BytesIO

from analytics_toolkit.sql.backends.ch.http_codec import ExactHTTPClient
from analytics_toolkit.sql.dml.transfer.io.source import iter_source_batches

from tests.sql._support.adaptive_transfer import Scenario, attempt, install
from tests.sql.backends.ch._support.exact_transport import http_query_context, http_source
from tests.sql.backends.ch._support.network_transport import encode_normalized


class LimitedHTTPSource:
    server_tz = "UTC"

    def __init__(self, values):
        from clickhouse_connect.driver.transform import NativeTransform  # noqa: PLC0415

        self._transform = NativeTransform()
        self.values = list(values)
        self.query_limit = 1
        self.limits_seen = []
        self.queries = []
        self.close_calls = 0
        self.response_close_calls = 0

    def query_column_block_stream(self, query, **_kwargs):
        from clickhouse_driver.writer import write_binary_str  # noqa: PLC0415

        self.queries.append(query)
        self.limits_seen.append(self.query_limit)
        values = self.values[: self.query_limit] if self.query_limit else self.values
        stream = BytesIO(bytes([1, len(values)]))
        stream.seek(2)
        write_binary_str("id", stream)
        write_binary_str("Int256", stream)
        stream.write(encode_normalized("Int256", values, "http"))
        result = self._transform.parse_response(
            http_source(stream.getvalue(), close=self.close_response),
            http_query_context(column_oriented=True, use_numpy=False),
        )
        return result.column_block_stream

    def close_response(self):
        self.response_close_calls += 1

    def close(self):
        self.close_calls += 1


def install_limited_http_transfer(monkeypatch, values):
    raw = LimitedHTTPSource(values)
    source = ExactHTTPClient(raw)

    def read(scenario, key, backend, ref, query, *args, **kwargs):
        scenario.read_count += 1
        scenario.events.append(f"read:{query}")
        yield from iter_source_batches(
            key,
            backend,
            ref,
            query,
            args[0] if args else scenario.options.batch_size,
            kwargs.pop("retry_cnt", scenario.options.retry_cnt),
            kwargs.pop("timeout_increment", scenario.options.timeout_increment),
            **kwargs,
        )
        scenario.events.append(f"complete:{query}")

    monkeypatch.setattr(Scenario, "read", read)
    scenario = install(monkeypatch, native_type="Int256", rows=[(value,) for value in values])
    connect_target = attempt.get_sql_connection

    def connect(alias):
        if alias == scenario.options.from_db_key:
            scenario.source_open_count += 1
            return source
        return connect_target(alias)

    monkeypatch.setattr(attempt, "get_sql_connection", connect)
    return scenario, source, raw
