"""Exact DateTime64 decoding preserves supported per-query SDK options."""

from __future__ import annotations

import importlib
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.http_codec import ExactHTTPClient

from tests.sql.backends.ch._support.http_read_context import ContextHTTPResponse

read_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")


def _sdk_timezone(raw: ContextHTTPResponse, native_type: str) -> str | None:
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415

    context = raw.contexts[-1]
    active = context.active_tz
    zone = active(get_from_name(native_type).tzinfo) if callable(active) else active
    return str(zone) if zone is not None else None


@pytest.mark.parametrize("nullable", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_http_datetime64_integer_query_format_returns_original_binary_ticks(
    nullable: bool, stream: bool
) -> None:
    spec = "DateTime64(9, 'Europe/Moscow')"
    if nullable:
        spec = f"Nullable({spec})"
    raw = ContextHTTPResponse(spec, query_formats={"DateTime64": "int"})
    client = ExactHTTPClient(raw)
    if stream:
        with client.query_df_stream("SELECT value") as frames:
            frame = next(frames)
            assert list(frames) == []
    else:
        frame = client.query_df("SELECT value")
    assert frame.iloc[0, 0] == -1
    assert frame.iloc[2, 0] == 1
    assert not isinstance(frame.iloc[0, 0], pd.Timestamp)
    if nullable:
        assert pd.isna(frame.iloc[1, 0])
    else:
        assert frame.iloc[1, 0] == 0
    assert raw.queries == ["SELECT value"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("timezone", ["UTC", "America/New_York"])
def test_http_datetime64_column_timezone_overrides_declared_timezone_without_tick_loss(
    stream: bool, timezone: str
) -> None:
    spec = "DateTime64(9, 'Europe/Moscow')"
    raw = ContextHTTPResponse(spec, query_tz="Asia/Tokyo", column_tzs={"value": timezone})
    client = ExactHTTPClient(raw)
    if stream:
        with client.query_df_stream("SELECT value") as frames:
            frame = next(frames)
            assert list(frames) == []
    else:
        frame = client.query_df("SELECT value")
    assert [value.value for value in frame.iloc[:, 0]] == [-1, 0, 1]
    expected = _sdk_timezone(raw, spec)
    assert all(
        (str(value.tz) if value.tz is not None else None) == expected for value in frame.iloc[:, 0]
    )


@pytest.mark.parametrize("declared", [False, True])
def test_http_datetime64_query_timezone_retains_sdk_declared_timezone_precedence(
    declared: bool,
) -> None:
    spec = "DateTime64(9, 'Europe/Moscow')" if declared else "DateTime64(9)"
    raw = ContextHTTPResponse(spec, query_tz="America/New_York")
    result = ExactHTTPClient(raw).query_df("SELECT value")
    assert [value.value for value in result.value] == [-1, 0, 1]
    assert all(str(value.tz) == _sdk_timezone(raw, spec) for value in result.value)


@pytest.mark.parametrize("stream", [False, True])
def test_http_datetime64_keeps_declared_utc_awareness_without_column_override(stream: bool) -> None:
    raw = ContextHTTPResponse("DateTime64(9, 'UTC')")
    client = ExactHTTPClient(raw)
    if stream:
        with client.query_df_stream("SELECT value") as frames:
            frame = next(frames)
            assert list(frames) == []
    else:
        frame = client.query_df("SELECT value")
    assert [value.value for value in frame.value] == [-1, 0, 1]
    assert all(str(value.tz) == "UTC" for value in frame.value)


@pytest.mark.parametrize("integer_format", [False, True])
def test_public_read_preserves_http_datetime64_query_context(
    monkeypatch: pytest.MonkeyPatch, integer_format: bool
) -> None:
    options: dict[str, Any] = {"column_tzs": {"value": "UTC"}}
    if integer_format:
        options["query_formats"] = {"DateTime64": "int"}
    raw = ContextHTTPResponse("DateTime64(9, 'Europe/Moscow')", **options)
    monkeypatch.setattr(read_module, "get_sql_connection", lambda _key: ExactHTTPClient(raw))
    result = sql.read("ch", "SELECT value", retry_cnt=1)
    if integer_format:
        assert result.value.tolist() == [-1, 0, 1]
    else:
        assert [value.value for value in result.value] == [-1, 0, 1]
        expected = _sdk_timezone(raw, "DateTime64(9, 'Europe/Moscow')")
        assert all(
            (str(value.tz) if value.tz is not None else None) == expected for value in result.value
        )
    assert len(raw.queries) == 1
    assert raw.close_calls == 1
