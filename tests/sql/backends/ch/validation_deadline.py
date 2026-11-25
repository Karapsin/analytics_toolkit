"""Driver progress and native-format reads share the finite validation budget."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends.ch.http_codec import ExactHTTPTransform
from analytics_toolkit.sql.backends.ch.native_client import NativeClickHouseClient
from analytics_toolkit.sql.connection import validation
from analytics_toolkit.sql.connection.validation import validation_scope

from tests.sql.backends.ch._support.exact_transport import BinaryNativeSource, native_context
from tests.sql.backends.ch._support.float_values import float_binary


def test_native_progress_decoder_checks_total_probe_budget_after_actual_packet_read(monkeypatch):
    from clickhouse_driver import Client  # noqa: PLC0415

    clock = [0.0]
    monkeypatch.setattr(validation, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    class AdvancingSource(BinaryNativeSource):
        def read(self, size=-1):
            result = super().read(size)
            clock[0] += 3.0
            return result

    raw = Client("127.0.0.1")
    NativeClickHouseClient(raw)
    raw.connection.server_info = native_context().server_info
    raw.connection.fin = AdvancingSource(b"\x01\x02")

    with validation_scope(), pytest.raises(TimeoutError, match="validation budget"):
        raw.connection.receive_progress()

    assert raw.connection.fin.tell() == 2


def test_http_binary_response_closes_owned_source_if_read_consumes_probe_deadline(monkeypatch):
    from clickhouse_connect.driver.ctypes import RespBuffCls  # noqa: PLC0415
    from clickhouse_connect.driver.query import QueryContext  # noqa: PLC0415
    from clickhouse_connect.driver.transform import NativeTransform  # noqa: PLC0415

    clock = [0.0]
    closed = []
    monkeypatch.setattr(validation, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    _spec, binary = float_binary(nested=False)

    def chunks():
        clock[0] = 6.0
        yield binary

    source = RespBuffCls(SimpleNamespace(gen=chunks(), close=lambda: closed.append(True)))
    context = QueryContext(use_numpy=False, use_none=True)

    with validation_scope(), pytest.raises(TimeoutError, match="validation budget"):
        ExactHTTPTransform(NativeTransform()).parse_response(source, context)

    assert closed == [True]
