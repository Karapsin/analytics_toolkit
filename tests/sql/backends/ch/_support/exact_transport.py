from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import Any


class BinaryNativeSource(BytesIO):
    def read_one(self) -> int:
        return self.read(1)[0]

    def read_strings(self, count: int, encoding: str = "utf-8") -> tuple[str, ...]:
        from clickhouse_driver.reader import read_binary_bytes  # noqa: PLC0415

        return tuple(read_binary_bytes(self).decode(encoding) for _ in range(count))


def native_context() -> Any:
    from clickhouse_driver.context import Context  # noqa: PLC0415

    context = Context()
    context.settings = {}
    context.client_settings = {
        "use_numpy": False,
        "strings_encoding": "utf-8",
        "strings_as_bytes": False,
    }
    context.server_info = SimpleNamespace(used_revision=0, get_timezone=lambda: "UTC")
    return context


def http_source(payload: bytes, close: Any = None, *, python_buffer: bool = False) -> Any:
    from clickhouse_connect.driver.buffer import ResponseBuffer  # noqa: PLC0415
    from clickhouse_connect.driver.ctypes import RespBuffCls  # noqa: PLC0415

    buffer_type = ResponseBuffer if python_buffer else RespBuffCls
    return buffer_type(SimpleNamespace(gen=iter([payload]), close=close or (lambda: None)))


def http_query_context(**kwargs: Any) -> Any:
    """Use the same UTC-naive fixture policy across HTTP driver generations."""
    from clickhouse_connect.driver.query import QueryContext  # noqa: PLC0415

    context = QueryContext(query_tz="UTC", **kwargs)
    if not callable(context.active_tz):
        context.query_tz = None
        context.active_tz = None
    return context
