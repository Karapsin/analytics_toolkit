"""Healthy-only DDL inference, identity reuse and acknowledged relay failures."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.outage_ddl import (
    _column_signature,
    create_on_healthy_hosts,
    create_target,
    local_create,
    provision_missing,
)
from analytics_toolkit.sql.backends.ch.outage_topology import ClusterScope, TableObject
from analytics_toolkit.sql.backends.ch.routing import _RoutingClickHouse
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.execution.backend_options import BackendOperationState, backend_operation
from sqlglot import exp, parse_one

from tests.sql._support.outage import Client


def _scope(client: Any) -> ClusterScope:
    return ClusterScope(
        client._client,
        get_connection_config("ch"),
        "core",
        BackendOperationState("healthy_shards"),
        "healthy_shards",
    )


@pytest.mark.parametrize("empty", [True, False])
def test_outage_create_as_infers_schema_and_populates_once(outage: Any, empty: bool) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    client = sql.get_sql_connection("ch", ch_unavailable_shards="healthy_shards")
    query = (
        "CREATE TABLE db.copied ON CLUSTER core ENGINE=MergeTree ORDER BY id "
        + ("EMPTY " if empty else "")
        + "AS SELECT id FROM db.events"
    )
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        client.command(query)
    table = cluster.tables[("h1", "db.copied")]
    assert table.rows == ([] if empty else [(1,)])
    assert "id UInt64" in table.ddl or '"id" UInt64' in table.ddl
    assert ("h2", "db.copied") not in cluster.tables
    assert all("ON CLUSTER" not in query for _, query, _ in cluster.commands)


def test_outage_create_as_table_and_invalid_expression(outage: Any) -> None:
    client = sql.get_sql_connection("ch")
    statement = parse_one(
        "CREATE TABLE db.copy ENGINE=MergeTree ORDER BY id AS db.events", read=_RoutingClickHouse
    )
    converted, query = client._expand_create(statement)
    assert isinstance(converted.this, exp.Schema)
    assert query == "SELECT * FROM db.events"
    statement.set("expression", exp.Literal.number(1))
    with pytest.raises(sql.ClickHouseClusterTopologyError):
        client._expand_create(statement)


@pytest.mark.parametrize(
    "statement",
    [
        exp.Create(this=exp.Table(this=exp.Anonymous(this="numbers", expressions=[]))),
        exp.Create(this=exp.to_table("events")),
    ],
)
def test_outage_create_requires_named_table_and_database(statement: Any) -> None:
    with pytest.raises(sql.ClickHouseClusterTopologyError):
        create_target(statement, None)


def test_outage_local_create_adds_properties_and_column_signature_requires_schema() -> None:
    statement = exp.Create(this=exp.to_table("db.events"), kind="TABLE")
    created = local_create(statement)
    assert created.args["exists"] is True
    assert created.args["properties"] is not None
    with pytest.raises(sql.ClickHouseClusterTopologyError):
        _column_signature(statement)


@pytest.mark.parametrize(
    "query",
    [
        "CREATE OR REPLACE TABLE db.x (id UInt64) ENGINE=MergeTree ORDER BY id",
        "CREATE VIEW db.x AS SELECT 1",
        "CREATE TABLE db.x ENGINE=MergeTree ORDER BY id AS SELECT 1",
        "CREATE TABLE db.events_shard (id UInt64) ENGINE=MergeTree ORDER BY id",
        "CREATE TABLE IF NOT EXISTS db.events_shard (id String) ENGINE=MergeTree ORDER BY id",
    ],
)
def test_outage_unsafe_creation_rejected_without_commands(outage: Any, query: str) -> None:
    cluster, _ = outage
    scope = _scope(sql.get_sql_connection("ch"))
    with pytest.raises(sql.ClickHouseClusterTopologyError):
        create_on_healthy_hosts(scope, parse_one(query, read=_RoutingClickHouse))
    assert cluster.commands == []


@pytest.mark.parametrize(
    "retained", ["SELECT 1", "CREATE TABLE db.new (id UInt64) ENGINE=MergeTree ORDER BY id"]
)
def test_outage_retained_identity_is_validated_and_reused(outage: Any, retained: str) -> None:
    cluster, _ = outage
    scope = _scope(sql.get_sql_connection("ch"))
    scope.state.resources[("ch_create", "ch", "core", "db.new")] = retained
    statement = parse_one(
        "CREATE TABLE db.new (id UInt64) ENGINE=MergeTree ORDER BY id", read=_RoutingClickHouse
    )
    if retained == "SELECT 1":
        with pytest.raises(sql.ClickHouseClusterTopologyError, match="Retained"):
            create_on_healthy_hosts(scope, statement)
        assert not cluster.commands
    else:
        assert create_on_healthy_hosts(scope, statement) is False
        assert ("h1", "db.new") in cluster.tables
        assert ("h2", "db.new") in cluster.tables


def test_outage_creation_requires_confirmed_schema_on_every_selected_host(
    outage: Any, monkeypatch: Any
) -> None:
    cluster, _ = outage
    scope = _scope(sql.get_sql_connection("ch"))
    monkeypatch.setattr(scope, "objects", lambda *args: [])
    statement = parse_one(
        "CREATE TABLE db.new (id UInt64) ENGINE=MergeTree ORDER BY id", read=_RoutingClickHouse
    )
    with pytest.raises(sql.ClickHouseClusterUnavailableError, match="verified"):
        create_on_healthy_hosts(scope, statement)
    assert ("h1", "db.new") in cluster.tables


def test_outage_recovery_refuses_unparseable_authoritative_schema(outage: Any) -> None:
    scope = _scope(sql.get_sql_connection("ch"))
    obj = TableObject(scope.hosts[0], "MergeTree", "MergeTree", "SELECT 1", "")
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="reconstruct"):
        provision_missing(scope, "db", "events", [obj])
    assert provision_missing(scope, "db", "events", []) == []


@pytest.mark.parametrize("live", [False, True])
def test_outage_relay_rejects_column_mismatch_and_retains_after_live_delivery(
    outage: Any, live: bool, monkeypatch: Any
) -> None:
    cluster, _ = outage
    client = sql.get_sql_connection("ch")
    batches = [pd.DataFrame({"unexpected": [3], "extra": [4]})]
    if live:
        batches.insert(0, pd.DataFrame({"id": [9]}))
    monkeypatch.setattr(
        client._client, "query_df_stream", lambda *args, **kwargs: nullcontext(iter(batches))
    )
    statement = parse_one(
        "INSERT INTO db.events (id) SELECT id FROM db.events", read=_RoutingClickHouse
    )
    with backend_operation() as state:
        with pytest.raises(sql.ClickHouseClusterTopologyError, match="column counts"):
            client._relay_insert(statement, state)
        assert state.preserve_recovery is live
    assert cluster.tables[("h1", "db.events_shard")].rows == ([(1,), (9,)] if live else [(1,)])


def test_outage_relay_rejects_non_named_target(outage: Any) -> None:
    client = sql.get_sql_connection("ch")
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="named"):
        client._relay_insert(exp.Insert(this=exp.Anonymous(this="remote")), BackendOperationState())


def test_outage_native_relay_does_not_query_occupied_stream_connection(
    outage: Any, monkeypatch: Any
) -> None:
    cluster, configure = outage
    configure(driver="native")
    cluster.alive.remove("h2")
    active: set[Client] = set()
    readers: list[Client] = []
    writers: list[Client] = []
    closed: list[Client] = []
    query, insert, close = Client.query, Client.insert_df, Client.close

    def checked_query(client: Client, *args: Any, **kwargs: Any) -> Any:
        assert client not in active, "native stream connection is occupied"
        return query(client, *args, **kwargs)

    def checked_insert(client: Client, *args: Any, **kwargs: Any) -> Any:
        assert client not in active, "native stream connection is occupied"
        writers.append(client)
        return insert(client, *args, **kwargs)

    @contextmanager
    def stream(client: Client, *args: Any, **kwargs: Any) -> Any:
        frame = client.query_df(*args, **kwargs)
        readers.append(client)
        active.add(client)
        try:
            yield iter([frame, frame])
        finally:
            active.remove(client)

    def checked_close(client: Client) -> None:
        closed.append(client)
        close(client)

    monkeypatch.setattr(Client, "query", checked_query)
    monkeypatch.setattr(Client, "insert_df", checked_insert)
    monkeypatch.setattr(Client, "query_df_stream", stream)
    monkeypatch.setattr(Client, "close", checked_close)
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        sql.execute(
            "ch",
            "INSERT INTO db.events SELECT id FROM db.events",
            ch_unavailable_shards="healthy_shards",
        )
    assert len(writers) == 2
    assert all(writer is not readers[0] for writer in writers)
    assert writers[0] in closed
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (1,), (1,)]


def test_outage_existing_indexes_are_preserved_and_not_mistaken_for_columns(outage: Any) -> None:
    cluster, _ = outage
    definition = (
        "CREATE TABLE db.indexed (id UInt64, INDEX by_id id TYPE minmax GRANULARITY 1) "
        "ENGINE=MergeTree ORDER BY id"
    )
    for host in cluster.hosts:
        cluster.create(host, definition)
    client = sql.get_sql_connection("ch", ch_unavailable_shards="healthy_shards")
    client.command(
        "CREATE TABLE IF NOT EXISTS db.indexed ON CLUSTER core (id UInt64) "
        "ENGINE=MergeTree ORDER BY id"
    )
    policy = type("Policy", (), {"shard_on_cluster": "core"})()
    assert client.outage_after_create(
        "db.indexed", policy=policy, pair=False, expected_columns={"id": "UInt64"}
    )
    assert "INDEX by_id" in cluster.tables[("h1", "db.indexed")].ddl


def test_outage_schema_inference_preserves_server_default_engine(outage: Any) -> None:
    client = sql.get_sql_connection("ch")
    statement = parse_one(
        "CREATE TABLE db.implicit AS SELECT id FROM db.events", read=_RoutingClickHouse
    )
    converted, query = client._expand_create(statement)
    assert isinstance(converted.this, exp.Schema)
    assert converted.args.get("properties") is None
    assert query == "SELECT id FROM db.events"


def test_outage_ctas_lost_acknowledgement_is_never_replayed_as_if_not_exists(
    outage: Any, monkeypatch: Any
) -> None:
    cluster, _ = outage
    original = Client.command

    def command(client: Any, query: str, **kwargs: Any) -> Any:
        result = original(client, query, **kwargs)
        statement = parse_one(query, read=_RoutingClickHouse)
        if isinstance(statement, exp.Create) and isinstance(statement.expression, exp.Query):
            cluster.tables[(client.host, "db.ctas_accepted")].rows = [(9,)]
            msg = "CTAS populated rows; acknowledgement lost"
            raise ConnectionError(msg)
        return result

    monkeypatch.setattr(Client, "command", command)
    with pytest.raises(ConnectionError, match="populated"):
        sql.execute(
            "ch",
            "CREATE TABLE IF NOT EXISTS db.ctas_accepted ON CLUSTER core "
            "ENGINE=MergeTree ORDER BY id AS SELECT id FROM db.events",
            retry_cnt=3,
            timeout_increment=0,
        )
    assert len(cluster.commands) == 1
    assert cluster.tables[("h1", "db.ctas_accepted")].rows == [(9,)]


def test_outage_create_or_replace_requires_complete_execution_hosts(outage: Any) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    with pytest.raises(sql.ClickHouseClusterUnavailableError):
        sql.execute(
            "ch",
            "CREATE OR REPLACE TABLE db.events_shard ON CLUSTER core (id UInt64) "
            "ENGINE=MergeTree ORDER BY id",
        )
    assert not cluster.commands
