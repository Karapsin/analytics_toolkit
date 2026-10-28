"""Transport boundaries, local control commands and recovery ownership."""

from __future__ import annotations

import sys
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch import outage_client as client_module
from analytics_toolkit.sql.backends.ch.outage_context import internal_io
from analytics_toolkit.sql.backends.ch.outage_session import OutageSession
from analytics_toolkit.sql.backends.ch.outage_statements import exchange_or_rename
from analytics_toolkit.sql.backends.ch.outage_topology import _completion_banner
from analytics_toolkit.sql.backends.ch.routing import _RoutingClickHouse, local_sql
from analytics_toolkit.sql.execution.backend_options import (
    BackendOperationState,
    backend_operation,
    public_backend_options,
)
from analytics_toolkit.sql.execution.plans import SqlOperationMetadata, SqlOperationResult
from sqlglot import exp, parse_one


def test_outage_public_transport_read_and_binary_variants(outage: Any) -> None:
    cluster, _ = outage
    client = sql.get_sql_connection("ch")
    assert client.query_df("SELECT id FROM db.events").id.tolist() == [1, 2]
    client.insert("db.events", [(9,)], column_names=["id"])
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (9,)]
    client.query("SELECT id FROM cluster('core', 'db', 'events')")
    assert "events_shard" in cluster.queries[-1]


def test_outage_internal_host_io_bypasses_outer_routing(outage: Any) -> None:
    cluster, _ = outage
    client = sql.get_sql_connection("ch")
    with internal_io():
        client.query("SELECT 1")
        client.insert("db.events", [(9,)], column_names=["id"])
    assert cluster.queries == ["SELECT 1"]
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (9,)]


def test_outage_physical_binary_target_uses_ddl_routing_default(outage: Any) -> None:
    cluster, configure = outage
    configure(cluster_routing=None)
    client = sql.get_sql_connection("ch")
    client.insert("db.events_shard", [(9,)], column_names=["id"])
    assert cluster.commands[-1][2]["distributed_foreground_insert"] == 1
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (9,)]


def test_outage_local_alias_does_not_add_cluster_work(outage: Any) -> None:
    cluster, configure = outage
    configure(cluster_routing=None, ddl_defaults=None)
    client = sql.get_sql_connection("ch")
    client.query("SELECT id FROM db.events_shard")
    client.command("CREATE TABLE db.local (id UInt64) ENGINE=MergeTree ORDER BY id")
    assert ("h1", "db.local") in cluster.tables
    client.command("INSERT INTO db.local VALUES (9)")
    client.insert("db.local", [(10,)], column_names=["id"])
    assert cluster.tables[("h1", "db.local")].rows == [(9,), (10,)]
    assert client.outage_table_exists("db.local")
    assert ("h2", "db.local") not in cluster.tables
    assert "ON CLUSTER" not in cluster.commands[-1][1]


def test_outage_owned_private_cleanup_remains_host_local(outage: Any) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    client = sql.get_sql_connection("ch")
    table = "db.x__replace_owned"
    with backend_operation("healthy_shards"):
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            client.command(
                f"CREATE TABLE {table} ON CLUSTER core (id UInt64) ENGINE=MergeTree ORDER BY id"
            )
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            client.command(f"DROP TABLE {table} ON CLUSTER core")
    assert ("h1", table) not in cluster.tables
    assert all("ON CLUSTER" not in query for _, query, _ in cluster.commands)


@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
def test_outage_local_metadata_retains_policy_checks(outage: Any, policy: str) -> None:
    client = sql.get_sql_connection("ch", ch_unavailable_shards=policy)
    with local_sql():
        result = client.query("SELECT _shard_num FROM clusterAllReplicas('core', system, one)")
    assert len(result.result_rows) == 2


def test_outage_raw_query_settings_cannot_disable_acknowledgement(outage: Any) -> None:
    cluster, _ = outage
    client = sql.get_sql_connection("ch")
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="Query setting"):
        client.command("INSERT INTO db.events VALUES (9)", settings={"async_insert": 1})
    assert not cluster.commands


def test_outage_permissive_exception_mode_is_rejected(outage: Any) -> None:
    cluster, configure = outage
    configure(
        settings={"skip_unavailable_shards_mode": "unavailable_or_exception_before_processing"}
    )
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="permissive"):
        sql.read("ch", "SELECT id FROM db.events")
    assert not cluster.commands


def test_outage_invalid_destructive_target_is_rejected_before_io(outage: Any) -> None:
    client = sql.get_sql_connection("ch")
    session = OutageSession(client._client, client._config, BackendOperationState())
    statement = exp.Drop(this=exp.Table(this=exp.Anonymous(this="numbers")))
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="static"):
        client._preflight_destructive_tables(session, statement, "core")


def test_outage_metadata_parser_failure_does_not_run_unscoped_query(
    outage: Any, monkeypatch: Any
) -> None:
    client = sql.get_sql_connection("ch")
    session = OutageSession(client._client, client._config, BackendOperationState())
    query = "SELECT name FROM system.tables WHERE name='events'"
    statement = parse_one(query, read=_RoutingClickHouse)
    monkeypatch.setattr(client_module, "parse_one", lambda *args, **kwargs: exp.Select())
    with local_sql(), pytest.raises(sql.ClickHouseClusterTopologyError, match="metadata"):
        client._metadata_scope_sql(session, statement, query)


def test_outage_completion_sink_failure_cannot_turn_success_into_retry(
    outage: Any, monkeypatch: Any
) -> None:
    class BrokenSink:
        @staticmethod
        def isatty() -> bool:
            return True

        @staticmethod
        def write(value: str) -> None:
            raise OSError(value)

    monkeypatch.setattr(
        sys,
        "stderr",
        BrokenSink(),
    )
    _completion_banner(BackendOperationState(), "ch", succeeded=True)


def test_outage_statement_parser_handles_empty_and_multiple_commands() -> None:
    assert exchange_or_rename("") is None
    assert exchange_or_rename("SELECT 1; SELECT 2") is None
    assert exchange_or_rename("EXCHANGE TABLES db.a AND db.b; RENAME TABLE db.c TO db.d") is None
    parsed = exchange_or_rename("RENAME TABLE db.a TO db.b, db.c TO db.d ON CLUSTER 'core'")
    assert parsed is not None
    assert len(list(parsed[0].find_all(exp.Table))) == 4
    assert parsed[1] == ["'core'"]


def test_outage_explicit_option_decorator_is_idempotent() -> None:
    def read_sql(*, ch_unavailable_shards: str | None = None) -> str | None:
        return ch_unavailable_shards

    assert public_backend_options(read_sql) is read_sql


def test_outage_result_lists_receive_operation_coverage_metadata() -> None:
    @public_backend_options
    def execute_create() -> list[SqlOperationResult]:
        return [SqlOperationResult(data=None, rows=None, metadata=SqlOperationMetadata())]

    with backend_operation("healthy_shards") as state:
        state.statuses[("ch", "core")] = {"alias": "ch", "cluster": "core", "complete": False}
        results = execute_create()
    assert results[0].metadata.data_complete is False


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id FROM cluster(concat('co', 're'), 'db', 'events')",
        "SELECT id FROM cluster('core', concat('d', 'b'), 'events')",
        "SELECT id FROM cluster('core')",
    ],
)
def test_outage_dynamic_cluster_function_scope_is_rejected(outage: Any, query: str) -> None:
    client = sql.get_sql_connection("ch")
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="static"):
        client.query(query)


def test_outage_strict_creation_stays_local_without_routing_override(outage: Any) -> None:
    cluster, configure = outage
    configure(cluster_routing=None)
    client = sql.get_sql_connection("ch")
    client.command("CREATE TABLE db.strict_created (id UInt64) ENGINE=MergeTree ORDER BY id")
    assert "ON CLUSTER" not in cluster.commands[-1][1]
    assert "distributed_ddl_output_mode" not in cluster.commands[-1][2]
    assert ("h1", "db.strict_created") in cluster.tables
    assert ("h2", "db.strict_created") not in cluster.tables


@pytest.mark.parametrize("failure", [False, True])
def test_outage_only_newly_owned_private_inserts_are_safe_to_discard(
    outage: Any, failure: bool
) -> None:
    cluster, _ = outage
    cluster.alive.remove("h2")
    client = sql.get_sql_connection("ch")
    table = "db.stage__replace_owned"
    with backend_operation("healthy_shards") as state:
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            client.command(
                f"CREATE TABLE {table} ON CLUSTER core (id UInt64) ENGINE=MergeTree ORDER BY id"
            )
        if failure:
            cluster.failure = ConnectionError("private stage acknowledgement lost")
            with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(ConnectionError):
                client.insert(table, [(9,)], column_names=["id"])
        else:
            with pytest.warns(sql.ClickHouseDegradedClusterWarning):
                client.insert(table, [(9,)], column_names=["id"])
        assert state.live_submitted is False
        assert state.preserve_recovery is False


def test_outage_if_not_exists_cannot_claim_existing_private_named_data(outage: Any) -> None:
    cluster, _ = outage
    for host in cluster.hosts:
        cluster.create(
            host, "CREATE TABLE db.saved__replace_data (id UInt64) ENGINE=MergeTree ORDER BY id"
        )
        cluster.tables[(host, "db.saved__replace_data")].rows = [(77,)]
    cluster.alive.remove("h2")
    client = sql.get_sql_connection("ch")
    with backend_operation("healthy_shards"):
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            client.command(
                "CREATE TABLE IF NOT EXISTS db.saved__replace_data ON CLUSTER core (id UInt64) "
                "ENGINE=MergeTree ORDER BY id"
            )
        with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
            sql.ClickHouseClusterUnavailableError
        ):
            client.command("DROP TABLE db.saved__replace_data ON CLUSTER core")
    assert cluster.tables[("h1", "db.saved__replace_data")].rows == [(77,)]
    assert all(not query.startswith("DROP") for _, query, _ in cluster.commands)


def test_outage_cluster_exists_false_does_not_assume_local_schema(outage: Any) -> None:
    client = sql.get_sql_connection("ch")
    assert client.outage_table_exists("db.absent") is False


def test_outage_system_metadata_can_have_full_ad_hoc_coverage(outage: Any) -> None:
    result = sql.read(
        "ch",
        "SELECT name FROM system.tables",
        ch_unavailable_shards="healthy_shards",
        return_metadata=True,
    )
    assert result.metadata.data_complete is True


def test_outage_statement_parser_preserves_local_exchange_scope() -> None:
    parsed = exchange_or_rename("EXCHANGE TABLES db.a AND db.b")
    assert parsed is not None
    assert parsed[1] == []
    assert [table.name for table in parsed[0].find_all(exp.Table)] == ["a", "b"]
