"""Coverage, schema readiness and settings contracts for outage-aware metadata."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.backends.ch.outage_session import OutageSession
from analytics_toolkit.sql.backends.ch.routing import _RoutingClickHouse, local_sql
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import SqlConfigError
from analytics_toolkit.sql.execution.backend_options import BackendOperationState
from sqlglot import exp, parse_one


def _session(client: Any, policy: str = "healthy_shards") -> OutageSession:
    return OutageSession(client._client, get_connection_config("ch"), BackendOperationState(policy))


@pytest.mark.parametrize(
    "query",
    [
        "SELECT count() FROM system.clusters WHERE cluster='core'",
        "SELECT name FROM system.tables WHERE database='db' AND name='events'",
        "SELECT name FROM system.columns WHERE database='db' AND table='events'",
        "DESCRIBE TABLE db.events",
    ],
)
def test_outage_metadata_is_scoped_to_selected_hosts(outage: Any, query: str) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    client = sql.get_sql_connection("ch")
    session = _session(client)
    statement = parse_one(query, read=_RoutingClickHouse)
    with local_sql():
        if query.startswith("DESCRIBE"):
            with pytest.warns(sql.ClickHouseDegradedClusterWarning):
                routed = client._metadata_scope_sql(session, statement, query)
            assert "events_shard" in routed
        else:
            if "system.clusters" in query:
                routed = client._metadata_scope_sql(session, statement, query)
            else:
                with pytest.warns(sql.ClickHouseDegradedClusterWarning):
                    routed = client._metadata_scope_sql(session, statement, query)
            assert "(1, 1)" in routed or "_shard_num = 1" in routed
    assert "h2" not in routed


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM system.clusters",
        "SELECT * FROM system.clusters WHERE shard_num=1",
        "SELECT * FROM system.tables",
        "SELECT * FROM system.tables WHERE database='db'",
        "SELECT * FROM system.tables WHERE name='absent'",
        "SELECT 1",
        "CREATE TABLE db.example (id UInt64) ENGINE=MergeTree ORDER BY id",
    ],
)
def test_outage_metadata_leaves_unrelated_queries_unchanged(outage: Any, query: str) -> None:
    client = sql.get_sql_connection("ch")
    session = _session(client)
    statement = parse_one(query, read=_RoutingClickHouse)
    with local_sql():
        assert client._metadata_scope_sql(session, statement, query) == query


def test_outage_metadata_requires_database_and_complete_data_coverage(outage: Any) -> None:
    cluster, _ = outage
    client = sql.get_sql_connection("ch")
    query = "SELECT name FROM system.tables WHERE name='events'"
    statement = parse_one(query, read=_RoutingClickHouse)
    session = _session(client)
    session.config = replace(session.config, database=None)
    client._config = session.config
    with local_sql():
        assert client._metadata_scope_sql(session, statement, query) == query
    session.config = replace(session.config, database="db")
    client._config = session.config
    del cluster.tables[("h2", "db.events_shard")]
    with local_sql(), pytest.raises(sql.ClickHouseClusterUnavailableError):
        client._metadata_scope_sql(_session(client, "stop"), statement, query)


@pytest.mark.parametrize(
    ("cluster_name", "table", "expected", "error"),
    [
        (None, "db.events", None, None),
        (None, "db.absent", None, sql.ClickHouseClusterUnavailableError),
        ("core", "db.events", {"id": "UInt64"}, None),
        ("core", "db.events", None, None),
        ("core", "db.events", {"id": "String"}, sql.ClickHouseClusterTopologyError),
        ("core", "db.absent", None, sql.ClickHouseClusterUnavailableError),
    ],
)
def test_outage_readiness_checks_schema_and_selected_scope(
    outage: Any, cluster_name: Any, table: str, expected: Any, error: Any
) -> None:
    client = sql.get_sql_connection("ch", ch_unavailable_shards="healthy_shards")
    policy = SimpleNamespace(shard_on_cluster=cluster_name, distributed_on_cluster=cluster_name)
    if error:
        with pytest.raises(error):
            client.outage_after_create(table, policy=policy, pair=False, expected_columns=expected)
    else:
        assert client.outage_after_create(
            table, policy=policy, pair=False, expected_columns=expected
        )
    assert client.outage_after_create(table, policy=None, pair=False) is False


def test_outage_readiness_does_not_relax_production_policy(outage: Any) -> None:
    client = sql.get_sql_connection("ch")
    assert client.outage_after_create("db.events", policy=object(), pair=False) is False


def test_outage_proxy_preserves_transport_attributes_and_raw_results(outage: Any) -> None:
    client = sql.get_sql_connection("ch")
    client.custom_option = 7
    client._private_option = 8
    assert client._client.custom_option == 7
    assert client.custom_option == 7
    assert client._private_option == 8
    assert client.cluster_routing.cluster == "core"
    client._client.raw_query = lambda query, **kwargs: (query, kwargs)
    query, kwargs = client.raw_query("SELECT id FROM db.events")
    assert "events_shard" in query
    assert kwargs["settings"]["skip_unavailable_shards"] == 0
    client.close()
    assert client._client.closed


def test_outage_local_alias_exists_and_missing_database_validation(outage: Any) -> None:
    _, configure = outage
    configure(cluster_routing=None, ddl_defaults=None)
    client = sql.get_sql_connection("ch")
    assert client.outage_table_exists("db.events")
    assert client.outage_table_exists("db.missing") is False
    configure(database=None)
    client = sql.get_sql_connection("ch")
    client._config = replace(client._config, database=None)
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="database"):
        client.outage_table_exists("events")


@pytest.mark.parametrize(
    "function",
    [
        exp.Anonymous(this="remote", expressions=[]),
        exp.Anonymous(this="cluster", expressions=[exp.Literal.string("core")]),
        exp.Anonymous(
            this="cluster",
            expressions=[
                exp.func("concat", "a", "b"),
                exp.to_identifier("db"),
                exp.to_identifier("events"),
            ],
        ),
    ],
)
def test_outage_function_targets_must_be_static_supported_routes(function: Any) -> None:
    with pytest.raises(sql.ClickHouseClusterTopologyError):
        OutageClient._function_target(exp.Insert(this=function))


def test_outage_function_target_accepts_schema_wrappers_and_default_random_key() -> None:
    function = exp.Anonymous(
        this="cluster",
        expressions=[
            exp.Literal.string("core"),
            exp.to_identifier("db"),
            exp.to_identifier("events"),
        ],
    )
    statement = exp.Insert(this=exp.Schema(this=exp.Table(this=function), expressions=[]))
    assert OutageClient._function_target(statement) == ("core", "db", "events", "rand()")


def test_outage_inline_boolean_settings_are_validated() -> None:
    statement = exp.select("1")
    statement.set(
        "settings",
        [
            exp.EQ(
                this=exp.to_identifier("skip_unavailable_shards"),
                expression=exp.Boolean(this=False),
            )
        ],
    )
    OutageClient._check_inline_settings(statement, {"skip_unavailable_shards": 0})
    with pytest.raises(sql.ClickHouseClusterTopologyError):
        OutageClient._check_inline_settings(statement, {"skip_unavailable_shards": 1})


@pytest.mark.parametrize("value", ["ignore", None, 1])
def test_outage_alias_default_is_validated_as_configuration_error(outage: Any, value: Any) -> None:
    _, configure = outage
    configure(ch_unavailable_shards=value)
    with pytest.raises(SqlConfigError, match="ch_unavailable_shards"):
        sql.get_sql_connection("ch")
