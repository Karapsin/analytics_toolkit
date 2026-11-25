"""Opening ClickHouse preserves codec failures and immutable setting profiles."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import clickhouse_connect
import clickhouse_driver
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection import validation


@pytest.mark.parametrize("entrypoint", ["connect", "validate"])
@pytest.mark.parametrize("close_fails", [False, True])
@pytest.mark.parametrize("transform", ["missing", "invalid"])
def test_http_codec_installation_failure_closes_opened_client_once_without_masking_error(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str, close_fails: bool, transform: str
) -> None:
    events = []

    class BrokenHTTP:
        __module__ = "clickhouse_connect.driver.fixture"

        def __init__(self) -> None:
            if transform == "invalid":
                self._transform = SimpleNamespace(parse_response=None)

        def close(self) -> None:
            events.append("close")
            if close_fails:
                message = "private-close-failure"
                raise OSError(message)

    opened = []

    def get_client(**_kwargs: Any) -> Any:
        client = BrokenHTTP()
        opened.append(client)
        return client

    monkeypatch.setattr(clickhouse_connect, "get_client", get_client)
    if entrypoint == "connect":
        with pytest.raises(RuntimeError, match=r"^Incompatible clickhouse-connect exact-decoding"):
            sql.get_sql_connection("ch")
    else:
        result = sql.validate_connections(["ch"], connect=True)[0]
        assert result.connected is False
        assert result.error == "Connectivity probe failed (RuntimeError)."
    assert len(opened) == 1
    assert events == ["close"]


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("configured_setting", [False, True])
def test_clickhouse_validation_consumes_select_one_without_changing_readonly_execution_profile(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Any,
    driver: str,
    configured_setting: bool,
) -> None:
    events = []
    opened = []
    real_native_client = clickhouse_driver.Client

    def factory(**kwargs: Any) -> Any:
        opened.append(kwargs)
        if kwargs.get("settings", {}).get("max_execution_time", 60) != 60:
            message = "The immutable profile forbids changing max_execution_time."
            raise PermissionError(message)
        if driver == "http":
            return SimpleNamespace(
                query=lambda statement, **_options: (
                    events.append(statement) or SimpleNamespace(result_rows=[(1,)])
                ),
                close=lambda: events.append("close"),
            )
        client = real_native_client("127.0.0.1")

        def execute(statement: str, **_kwargs: Any) -> Any:
            events.append(statement)
            return [(1,)], [("1", "UInt8")]

        client.execute = execute
        client.disconnect_connection = lambda: events.append("close")
        return client

    target = {
        "type": "ch",
        "driver": driver,
        "host": "fixture",
        "user": "fixture",
        "password": "",
        "database": "default",
        "connect_timeout": 2,
        "send_receive_timeout": 3,
    }
    if configured_setting:
        target["settings"] = {"max_execution_time": 60}
    write_sql_connections({"target": target})
    monkeypatch.setattr(clickhouse_connect, "get_client", factory)
    monkeypatch.setattr(clickhouse_driver, "Client", factory)

    result = sql.validate_connections(["target"], connect=True)[0]

    assert result.connected is True
    assert result.error is None
    assert events == ["SELECT 1", "close"]
    assert len(opened) == 1
    expected = {"max_execution_time": 60} if configured_setting else {}
    assert opened[0].get("settings", {}) == expected
    assert 0 < opened[0]["connect_timeout"] <= 2
    assert 0 < opened[0]["send_receive_timeout"] <= 3


@pytest.mark.parametrize("driver", ["http", "native"])
def test_clickhouse_validation_does_not_submit_probe_after_opening_consumes_total_budget(
    monkeypatch: pytest.MonkeyPatch, write_sql_connections: Any, driver: str
) -> None:
    clock = [0.0]
    events = []
    real_native_client = clickhouse_driver.Client
    monkeypatch.setattr(validation, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def factory(**_kwargs: Any) -> Any:
        clock[0] = 6.0
        if driver == "http":
            return SimpleNamespace(
                query=lambda statement, **_options: (
                    events.append(statement) or SimpleNamespace(result_rows=[(1,)])
                ),
                close=lambda: events.append("close"),
            )
        client = real_native_client("127.0.0.1")
        client.execute = lambda statement, **_kw: (
            events.append(statement)
            or (
                [(1,)],
                [("1", "UInt8")],
            )
        )
        client.disconnect_connection = lambda: events.append("close")
        return client

    write_sql_connections(
        {
            "target": {
                "type": "ch",
                "driver": driver,
                "host": "fixture",
                "user": "fixture",
                "password": "",
                "database": "default",
            }
        }
    )
    monkeypatch.setattr(clickhouse_connect, "get_client", factory)
    monkeypatch.setattr(clickhouse_driver, "Client", factory)

    result = sql.validate_connections(["target"], connect=True)[0]

    assert result.connected is False
    assert result.error == "Connectivity probe failed (TimeoutError)."
    assert events == ["close"]


@pytest.mark.parametrize("driver", ["http", "native"])
def test_clickhouse_probe_keeps_connect_limit_separate_from_total_read_budget(
    monkeypatch: pytest.MonkeyPatch, write_sql_connections: Any, driver: str
) -> None:
    clock = [0.0]
    events = []
    opened = []
    real_native_client = clickhouse_driver.Client
    monkeypatch.setattr(validation, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def query(statement: str, **_options: Any) -> Any:
        events.append(statement)
        clock[0] += 1.5
        return [(1,)], [("1", "UInt8")]

    def factory(**kwargs: Any) -> Any:
        opened.append(kwargs)
        clock[0] += 1.5
        if driver == "http":

            def http_query(statement: str, **options: Any) -> Any:
                rows, _header = query(statement, **options)
                return SimpleNamespace(result_rows=rows)

            return SimpleNamespace(query=http_query, close=lambda: events.append("close"))
        client = real_native_client("127.0.0.1")
        client.execute = query
        client.disconnect_connection = lambda: events.append("close")
        return client

    write_sql_connections(
        {
            "target": {
                "type": "ch",
                "driver": driver,
                "host": "fixture",
                "user": "fixture",
                "password": "",
                "database": "default",
                "connect_timeout": 2,
                "send_receive_timeout": 25,
            }
        }
    )
    monkeypatch.setattr(clickhouse_connect, "get_client", factory)
    monkeypatch.setattr(clickhouse_driver, "Client", factory)

    result = sql.validate_connections(["target"], connect=True)[0]

    assert result.connected is True
    assert result.error is None
    assert events == ["SELECT 1", "close"]
    assert clock[0] == 3.0
    assert len(opened) == 1
    assert opened[0]["connect_timeout"] == 2
    assert opened[0]["send_receive_timeout"] == 5
