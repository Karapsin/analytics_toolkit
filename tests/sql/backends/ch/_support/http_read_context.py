"""HTTP query responses retain the actual SDK context and binary decoder."""

from __future__ import annotations

from io import BytesIO
from struct import pack
from typing import Any

from tests.sql.backends.ch._support.exact_transport import http_source


class ContextHTTPResponse:
    def __init__(self, spec: str, **context_options: Any) -> None:
        from clickhouse_connect.driver.transform import NativeTransform  # noqa: PLC0415
        from clickhouse_driver.writer import write_binary_str  # noqa: PLC0415

        self._transform = NativeTransform()
        self.context_options = context_options
        self.close_calls = 0
        self.queries: list[str] = []
        self.contexts: list[Any] = []
        output = BytesIO(b"\x01\x03")
        output.seek(2)
        write_binary_str("value", output)
        write_binary_str(spec, output)
        if spec.startswith("Nullable("):
            output.write(b"\x00\x01\x00")
        output.write(pack("<3q", -1, 0, 1))
        self.binary = output.getvalue()

    def query(self, statement: str, **kwargs: Any) -> Any:
        from clickhouse_connect.driver.query import QueryContext  # noqa: PLC0415

        self.queries.append(statement)
        options = {
            "use_numpy": False,
            "use_none": True,
            "column_oriented": kwargs.get("column_oriented", False),
            **self.context_options,
        }
        context = QueryContext(**options)
        self.contexts.append(context)
        return self._transform.parse_response(http_source(self.binary), context)

    def query_column_block_stream(self, statement: str, **_kwargs: Any) -> Any:
        return self.query(statement, column_oriented=True).column_block_stream

    def close(self) -> None:
        self.close_calls += 1
