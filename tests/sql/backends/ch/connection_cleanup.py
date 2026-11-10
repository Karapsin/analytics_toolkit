from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.lifecycle import (
    ChDistributedTablePair,
    _drop_ch_distributed_table_pair_on_host,
)
from analytics_toolkit.sql.backends.ch.outage_hosts import host_connection
from analytics_toolkit.sql.backends.ch.outage_topology import ClusterScope
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.execution.backend_options import backend_operation

from tests.sql._support.outage import Client, Cluster


@pytest.fixture
def relay_close_failure(
    outage: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[Cluster, list[Client]]:
    cluster, _ = outage
    cluster.alive.remove("h2")
    writers: list[Client] = []
    insert, close = Client.insert, Client.close
    close_error = RuntimeError("delivery connection close failed")

    def tracked_insert(
        client: Client, table: str, data: Any, column_names: Any, **kwargs: Any
    ) -> None:
        writers.append(client)
        insert(client, table, data, column_names, **kwargs)

    def failing_close(client: Client) -> None:
        close(client)
        if client in writers:
            raise close_error

    monkeypatch.setattr(Client, "insert", tracked_insert)
    monkeypatch.setattr(Client, "close", failing_close)
    return cluster, writers


def test_accepted_relay_is_not_replayed_when_delivery_close_fails(
    relay_close_failure: tuple[Cluster, list[Client]],
) -> None:
    cluster, writers = relay_close_failure
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        result = sql.execute(
            "ch",
            "INSERT INTO db.events SELECT id FROM db.events",
            ch_unavailable_shards="healthy_shards",
            retry_cnt=3,
            timeout_increment=0,
        )

    assert result is None
    assert len(writers) == 1
    assert writers[0].closed
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (1,)]


def test_relay_primary_error_survives_delivery_close_and_retains_live_write_guard(
    relay_close_failure: tuple[Cluster, list[Client]],
) -> None:
    cluster, writers = relay_close_failure
    primary = TimeoutError("insert acknowledgement was lost")
    cluster.failure = primary

    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(TimeoutError) as caught:
        sql.execute(
            "ch",
            "INSERT INTO db.events SELECT id FROM db.events",
            ch_unavailable_shards="healthy_shards",
            retry_cnt=3,
            timeout_increment=0,
        )

    assert caught.value is primary
    assert primary.analytics_toolkit_sql_retry_safe is False  # type: ignore[attr-defined]
    assert any("close_delivery" in note for note in getattr(primary, "__notes__", ()))
    assert len(writers) == 1
    assert writers[0].closed
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (1,)]


@pytest.mark.parametrize("operation_fails", [False, True])
def test_host_connection_close_preserves_success_and_primary_error(
    outage: Any, monkeypatch: pytest.MonkeyPatch, operation_fails: bool
) -> None:
    cluster, _ = outage
    client = sql.get_sql_connection("ch")
    close_calls: list[Client] = []
    close = Client.close
    close_error = RuntimeError("selected host connection close failed")

    def failing_close(connection: Client) -> None:
        close_calls.append(connection)
        close(connection)
        raise close_error

    with backend_operation() as state:
        scope = ClusterScope(client._client, get_connection_config("ch"), "core", state, "stop")
        remote = next(host for host in scope.hosts if not host.is_local)
        monkeypatch.setattr(Client, "close", failing_close)
        primary = TimeoutError("host acknowledgement was lost")
        if operation_fails:
            cluster.failure = primary
            with pytest.raises(TimeoutError) as caught, host_connection(
                scope, remote
            ) as connection:
                connection.command("INSERT INTO db.events VALUES (9)")
            assert caught.value is primary
            assert any("close_host" in note for note in getattr(primary, "__notes__", ()))
        else:
            with host_connection(scope, remote) as connection:
                connection.command("INSERT INTO db.events VALUES (9)")

    assert len(close_calls) == 1
    assert close_calls[0].closed
    assert cluster.tables[("h2", "db.events_shard")].rows == [(2,), (9,)]


@pytest.mark.parametrize("operation_fails", [False, True])
def test_per_host_drop_close_preserves_completion_and_host_error_contract(
    operation_fails: bool,
) -> None:
    commands: list[str] = []
    closed: list[bool] = []
    primary = TimeoutError("drop acknowledgement was lost")
    close_error = RuntimeError("drop connection close failed")

    def command(query: str, **_kwargs: Any) -> None:
        commands.append(query)
        if operation_fails:
            raise primary

    def close() -> None:
        closed.append(True)
        raise close_error

    connection = SimpleNamespace(command=command, close=close)
    result = _drop_ch_distributed_table_pair_on_host(
        "h2",
        pair=ChDistributedTablePair("db.events", "db.events_shard"),
        query_label=None,
        per_host_connection_factory=lambda _host: connection,
    )

    if operation_fails:
        assert result == f"h2: {primary!r}"
        assert commands == ["DROP TABLE IF EXISTS db.events"]
        assert any("close_host" in note for note in getattr(primary, "__notes__", ()))
    else:
        assert result is None
        assert commands == [
            "DROP TABLE IF EXISTS db.events",
            "DROP TABLE IF EXISTS db.events_shard",
        ]
    assert closed == [True]
