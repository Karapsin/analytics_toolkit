"""Decoded native NaN remains distinct from source SQL NULL and signed zero."""

from __future__ import annotations

import importlib

import pytest
from analytics_toolkit import sql

from tests.sql.backends.ch._support.float_values import assert_native_float_values, float_client

read_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")


@pytest.mark.parametrize("transport", ["native", "http"])
@pytest.mark.parametrize("nested", [False, True])
def test_public_clickhouse_read_distinguishes_binary_nan_from_sql_null(
    monkeypatch, transport, nested
):
    client, state, _spec = float_client(transport, nested=nested)
    monkeypatch.setattr(read_module, "get_sql_connection", lambda _key: client)

    result = sql.read("ch", "SELECT id", retry_cnt=1)

    values = result.iloc[0, 0] if nested else result.id.tolist()
    assert_native_float_values(values)
    assert state.query_count == 1
    assert state.close_calls == 1


@pytest.mark.parametrize("transport", ["native", "http"])
@pytest.mark.parametrize("nested", [False, True])
def test_clickhouse_stream_preserves_nan_null_and_signed_zero_through_positional_frames(
    transport, nested
):
    client, state, _spec = float_client(transport, nested=nested)
    with client.query_df_stream("SELECT id") as frames:
        frame = next(frames)
        assert list(frames) == []
    values = frame.iloc[0, 0] if nested else frame.id.tolist()
    assert_native_float_values(values)
    assert state.query_count == 1
