"""Default stop keeps unconfigured routing and native command semantics intact."""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp
from sqlglot.errors import ParseError

from tests.sql._support.outage import Client, Result


def test_outage_native_function_insert_keeps_its_target_and_uses_healthy_delivery(
    outage: Any,
) -> None:
    cluster, configure = outage
    configure(cluster_routing=None)
    cluster.alive.remove("h2")
    query = "INSERT INTO FUNCTION cluster('core', 'db', 'events_shard', rand()) VALUES (9)"
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        sql.execute("ch", query, ch_unavailable_shards="healthy_shards", retry_cnt=1)
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (9,)]
    assert cluster.tables[("h2", "db.events_shard")].rows == [(2,)]
    assert cluster.commands[-1][1] == query
    assert cluster.commands[-1][2]["insert_shard_id"] == 1


def test_outage_no_routing_alias_keeps_distributed_and_local_join_sql_unchanged(
    outage: Any, monkeypatch: Any
) -> None:
    cluster, configure = outage
    configure(cluster_routing=None)
    cluster.create("h1", "CREATE TABLE db.dimension (id UInt64) ENGINE=Memory")
    query = "SELECT e.id FROM db.events AS e JOIN db.dimension AS d ON e.id = d.id"
    submitted = []

    original = Client.query

    def read(client: Any, statement: str, **kwargs: Any) -> Any:
        if " JOIN " in statement:
            submitted.append(statement)
            return Result([(1,), (2,)])
        return original(client, statement, **kwargs)

    monkeypatch.setattr(Client, "query", read)
    result = sql.read("ch", query, return_metadata=True, retry_cnt=1)
    assert result.data.id.tolist() == [1, 2]
    assert result.metadata.data_complete is True
    assert submitted == [query]
    assert ("h2", "db.dimension") not in cluster.tables


def test_outage_native_cte_name_does_not_trigger_a_shadowed_distributed_table(
    outage: Any, monkeypatch: Any
) -> None:
    cluster, configure = outage
    configure(cluster_routing=None)
    cluster.alive.remove("h2")
    client = sql.get_sql_connection("ch")
    query = "WITH events AS (SELECT 1 AS id) SELECT id FROM events"
    submitted = []
    monkeypatch.setattr(
        client._client, "query", lambda statement, **_kwargs: submitted.append(statement)
    )
    client.query(query)
    assert submitted == [query]


@pytest.mark.parametrize(
    "statement",
    [
        "OPTIMIZE TABLE db.events_shard FINAL",
        "SYSTEM FLUSH LOGS",
        "KILL QUERY WHERE query_id='example' SYNC",
        "-- native empty request",
    ],
)
@pytest.mark.parametrize("fails", [False, True])
def test_outage_native_commands_and_their_errors_are_passthrough(
    outage: Any, monkeypatch: Any, statement: str, fails: bool
) -> None:
    _cluster, configure = outage
    configure(cluster_routing=None)
    client = sql.get_sql_connection("ch")
    submitted = []
    original_error = ValueError("native server error")

    def command(value: str, **_kwargs: Any) -> Any:
        submitted.append(value)
        if fails:
            raise original_error
        return "native result"

    monkeypatch.setattr(client._client, "command", command)
    if fails:
        with pytest.raises(ValueError, match="native server error") as error:
            client.command(statement)
        assert error.value is original_error
    else:
        assert client.command(statement) == "native result"
    assert submitted == [statement]


def test_outage_native_batch_survives_a_sqlglot_dialect_gap(outage: Any, monkeypatch: Any) -> None:
    _cluster, configure = outage
    configure(cluster_routing=None)
    module = importlib.import_module("analytics_toolkit.sql.backends.ch.outage_client")
    original = module.parse_one
    submitted = []

    def parse(statement: str, **kwargs: Any) -> Any:
        if statement == "SELECT 1":
            message = "simulated newer-server dialect"
            raise ParseError(message)
        return original(statement, **kwargs)

    monkeypatch.setattr(module, "parse_one", parse)
    monkeypatch.setattr(
        Client, "command", lambda _client, value, **_kwargs: submitted.append(value)
    )
    sql.execute("ch", "SELECT 1", retry_cnt=1)
    assert submitted == ["SELECT 1"]


def test_outage_native_deep_sql_is_not_rejected_by_frontend_grouping_limits(
    outage: Any, monkeypatch: Any
) -> None:
    _cluster, configure = outage
    configure(cluster_routing=None)
    client = sql.get_sql_connection("ch")
    module = importlib.import_module("analytics_toolkit.sql.backends.ch.outage_client")

    def parser(*_args: Any, **_kwargs: Any) -> Any:
        raise RecursionError

    monkeypatch.setattr(module, "parse_one", parser)
    submitted = []
    monkeypatch.setattr(client._client, "command", submitted.append)
    query = "SELECT " + "(" * 200 + "1" + ")" * 200
    client.command(query)
    assert submitted == [query]


def test_outage_native_cluster_command_still_stops_before_submission(
    outage: Any, monkeypatch: Any
) -> None:
    cluster, configure = outage
    configure(cluster_routing=None, ddl_defaults=None)
    cluster.alive.remove("h2")
    client = sql.get_sql_connection("ch")
    submitted = []
    monkeypatch.setattr(client._client, "command", submitted.append)
    with pytest.raises(sql.ClickHouseClusterUnavailableError):
        client.command("OPTIMIZE TABLE db.events_shard ON CLUSTER core FINAL")
    assert submitted == []


@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
def test_outage_generic_native_show_preserves_stop_but_rejects_unverified_ad_hoc_scope(
    outage: Any, monkeypatch: Any, policy: str
) -> None:
    _cluster, configure = outage
    configure(cluster_routing=None)
    client = sql.get_sql_connection("ch", ch_unavailable_shards=policy)
    module = importlib.import_module("analytics_toolkit.sql.backends.ch.outage_client")
    monkeypatch.setattr(module, "parse_one", lambda *_args, **_kwargs: exp.Command(this="SHOW"))
    submitted = []
    monkeypatch.setattr(client._client, "command", submitted.append)
    if policy == "stop":
        client.command("SHOW native_extension")
        assert submitted == ["SHOW native_extension"]
    else:
        with pytest.raises(sql.ClickHouseClusterTopologyError, match="cannot be verified"):
            client.command("SHOW native_extension")
        assert not submitted


def test_outage_routed_alias_retains_legacy_command_parser_rejection(outage: Any) -> None:
    cluster, _configure = outage
    with pytest.raises(InvalidSqlInputError, match="does not support"):
        sql.get_sql_connection("ch").command("SYSTEM FLUSH LOGS")
    assert not cluster.commands


def test_outage_ad_hoc_parser_errors_are_not_submitted_as_native_sql(outage: Any) -> None:
    cluster, configure = outage
    configure(cluster_routing=None)
    with pytest.raises(ParseError):
        sql.get_sql_connection("ch", ch_unavailable_shards="healthy_shards").command("SELECT (")
    assert not cluster.commands


def test_outage_native_scope_must_be_statically_verifiable(outage: Any) -> None:
    cluster, configure = outage
    configure(cluster_routing=None)
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="scope"):
        sql.get_sql_connection("ch").command("OPTIMIZE TABLE db.events ON CLUSTER (core)")
    assert not cluster.commands


def test_outage_batch_native_preflight_has_no_command_side_effects(outage: Any) -> None:
    cluster, configure = outage
    configure(cluster_routing=None)
    client = sql.get_sql_connection("ch")
    client.sql_preflight_statements(["SYSTEM FLUSH LOGS"])
    assert not cluster.commands
    assert "core" in client._session.scopes


def test_outage_native_table_functions_are_not_automatically_routed(outage: Any) -> None:
    cluster, configure = outage
    configure(cluster_routing=None)
    sql.get_sql_connection("ch").query("SELECT number FROM numbers(1)")
    assert cluster.queries[-1] == "SELECT number FROM numbers(1)"
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="static"):
        sql.get_sql_connection("ch").query(
            "SELECT id FROM cluster(concat('co','re'), 'db', 'events')"
        )
