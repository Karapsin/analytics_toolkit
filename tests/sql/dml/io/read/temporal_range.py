from __future__ import annotations

import datetime as dt
import importlib
import struct
from io import BytesIO
from types import SimpleNamespace

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.http_codec import exact_http_type
from analytics_toolkit.sql.backends.ch.native_codec import read_column

from tests.sql.backends.ch._support.exact_transport import (
    http_query_context,
    http_source,
    native_context,
)

read_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")


@pytest.mark.parametrize("transport", ["native", "http"])
def test_public_clickhouse_read_preserves_datetime64_microseconds_beyond_nanosecond_range(
    monkeypatch, transport
) -> None:
    expected = dt.datetime(2299, 12, 31, 23, 59, 59, 123456, tzinfo=dt.timezone.utc)
    delta = expected - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    ticks = (delta.days * 86400 + delta.seconds) * 10**6 + delta.microseconds
    payload = b"\x00\x01" + struct.pack("<2q", ticks, 0)
    native_type = "Nullable(DateTime64(6, 'UTC'))"
    if transport == "native":
        values = read_column(native_context(), native_type, 2, BytesIO(payload))
    else:
        from clickhouse_connect.datatypes.registry import (  # noqa: PLC0415
            get_from_name,  # selected real SDK codec.
        )

        datatype = exact_http_type(get_from_name(native_type))
        values = datatype.read_column(
            http_source(payload), 2, http_query_context(use_none=True, use_numpy=False)
        )
    assert values[0].isoformat() == expected.isoformat()
    assert values[1] is None
    queries = []
    closed = []

    class Client:
        def query(self, statement, *, column_oriented):
            assert column_oriented is True
            queries.append(statement)
            return SimpleNamespace(
                column_names=("event_at",), result_columns=(values,), row_count=2
            )

        def close(self):
            closed.append(True)

    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda _key: Client())

    frame = sql.read("ch", "SELECT event_at FROM sandbox.events", retry_cnt=1)

    assert frame.shape == (2, 1)
    assert frame.columns.tolist() == ["event_at"]
    assert frame.iloc[0, 0].isoformat() == expected.isoformat()
    assert frame.iloc[0, 0].microsecond == 123456
    assert pd.isna(frame.iloc[1, 0])
    assert queries == ["SELECT event_at FROM sandbox.events"]
    assert closed == [True]
