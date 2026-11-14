from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends.ch.reconfigure_topology import (
    DestinationTopology,
    validate_destination,
)

from tests.sql._support.reconfigure import (
    FakeClickHouseResult,
    InvalidSqlInputError,
    ReconfigureClient,
    reconfigure_api,
    reconfigure_backend,
    sql,
)


def _public_client(monkeypatch, client):
    monkeypatch.setattr(
        reconfigure_api,
        "get_connection_config",
        lambda key: SimpleNamespace(connection_key=key, backend="ch"),
    )
    monkeypatch.setattr(reconfigure_api, "get_sql_connection", lambda _key: client)
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_args: None)
    monkeypatch.setattr(reconfigure_backend, "_wait_for_cleanup", lambda *_args: None)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    "change",
    [{"ch_engine": "MergeTree"}, {"ch_settings": {"index_granularity": 4096}}],
    ids=["structural-rebuild", "facade-and-settings-rebuild"],
)
@pytest.mark.parametrize(
    "routing_hosts",
    [
        (("archive", "10.0.0.2", 9000),),
        (("same-host-other-instance", "10.0.0.1", 9001),),
    ],
    ids=["disjoint-host", "different-native-port"],
)
def test_same_physical_rebuild_rejects_wrong_copy_route_before_any_ddl(
    monkeypatch, dry_run, change, routing_hosts
):
    client = ReconfigureClient(target_hosts=routing_hosts)
    _public_client(monkeypatch, client)
    with pytest.raises(InvalidSqlInputError, match="replacement storage endpoints"):
        sql.ch_reconfigure_table(
            "ch",
            "analytics.events",
            ch_distributed_cluster="archive",
            ch_distributed_on_cluster="core",
            retry_cnt=1,
            timeout_increment=0,
            dry_run=dry_run,
            **change,
        )
    assert client.commands == []
    assert client.closed


@pytest.mark.parametrize("routing", ["core", "archive", "{cluster}", "{copy_route}"])
def test_same_physical_rebuild_accepts_verified_endpoint_aliases_and_macros(monkeypatch, routing):
    class AliasClient(ReconfigureClient):
        def query(self, statement):
            if "getMacro('copy_route')" in statement:
                self.queries.append(statement)
                return FakeClickHouseResult([("archive",)])
            return super().query(statement)

    client = AliasClient(target_hosts=(("source-alias", "10.0.0.1", 9000),))
    _public_client(monkeypatch, client)
    result = sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        ch_engine="MergeTree",
        ch_distributed_cluster=routing,
        ch_distributed_on_cluster="core",
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )
    assert result.data["strategy"] == "managed_pair_rebuild"
    assert any(statement.startswith("CREATE TABLE") for statement in client.commands)
    assert any("FROM system.clusters" in statement for statement in client.queries)
    assert client.closed


@pytest.mark.parametrize("wrapper_only", [False, True], ids=["no-op", "facade-only"])
def test_plans_without_physical_copy_do_not_query_endpoint_or_extra_database_metadata(
    monkeypatch, wrapper_only
):
    class NoEndpointClient(ReconfigureClient):
        def query(self, statement):
            if "FROM system.clusters" in statement:
                pytest.fail("A plan without physical copying must not inspect endpoint coverage")
            return super().query(statement)

    client = NoEndpointClient()
    _public_client(monkeypatch, client)
    change = (
        {
            "ch_sharding_key": "cityHash64(id)",
            "ch_distributed_cluster": "archive",
            "ch_distributed_on_cluster": "core",
        }
        if wrapper_only
        else {"ch_engine": "ReplicatedMergeTree('/clickhouse/{table}', '{replica}')"}
    )
    result = sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        retry_cnt=1,
        timeout_increment=0,
        validate_row_count=False,
        return_metadata=True,
        **change,
    )
    assert result.data["strategy"] == ("wrapper_recreate" if wrapper_only else "no_op")
    assert (
        len([statement for statement in client.queries if "FROM system.databases" in statement])
        == 1
    )
    assert client.closed


def test_physical_copy_without_a_verified_placement_fails_closed():
    topology = DestinationTopology(True, True, "core", None, "core", "db.events_shard", False, True)
    with pytest.raises(InvalidSqlInputError, match="Cannot verify replacement storage routing"):
        validate_destination(object(), topology)


@pytest.mark.parametrize(
    ("source_hosts", "target_hosts", "strategy"),
    [
        (
            (("source", "10.0.0.1", 9000),),
            (("source-alias", "10.0.0.1", 9000),),
            "managed_pair_rebuild",
        ),
        (
            (("source-a", "10.0.0.1", 9000), ("source-b", "10.0.0.2", 9000)),
            (("alias-b", "10.0.0.2", 9000), ("alias-a", "10.0.0.1", 9000)),
            "managed_pair_rebuild",
        ),
        (
            (("same-host", "10.0.0.1", 9000),),
            (("same-host", "10.0.0.1", 9001),),
            "cross_cluster_rebuild",
        ),
        (
            (("same-host", "", 9000),),
            (("same-host", "", 9000),),
            "managed_pair_rebuild",
        ),
    ],
    ids=[
        "dns-alias",
        "reordered-endpoint-aliases",
        "distinct-native-port",
        "literal-host-fallback",
    ],
)
def test_physical_cluster_aliases_use_resolved_endpoints_not_dns_labels(
    monkeypatch, source_hosts, target_hosts, strategy
):
    client = ReconfigureClient(source_hosts=source_hosts, target_hosts=target_hosts)
    _public_client(monkeypatch, client)
    result = sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        ch_engine="MergeTree",
        ch_shard_on_cluster="archive",
        ch_distributed_cluster="archive",
        ch_distributed_on_cluster="core",
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )
    assert result.data["strategy"] == strategy
    assert any(statement.startswith("CREATE TABLE") for statement in client.commands)
    assert client.closed


@pytest.mark.parametrize("empty_scope", ["source", "target"])
def test_physical_cluster_aliases_reject_empty_endpoint_metadata_before_ddl(
    monkeypatch, empty_scope
):
    client = ReconfigureClient(
        source_hosts=() if empty_scope == "source" else (("source", "10.0.0.1", 9000),),
        target_hosts=() if empty_scope == "target" else (("target", "10.0.0.2", 9000),),
    )
    _public_client(monkeypatch, client)
    with pytest.raises(InvalidSqlInputError, match="has no hosts"):
        sql.ch_reconfigure_table(
            "ch",
            "analytics.events",
            ch_engine="MergeTree",
            ch_shard_on_cluster="archive",
            ch_distributed_cluster="archive",
            ch_distributed_on_cluster="core",
            retry_cnt=1,
            timeout_increment=0,
        )
    assert client.commands == []
    assert client.closed


def test_physical_cluster_aliases_reject_partial_endpoint_overlap_before_ddl(monkeypatch):
    client = ReconfigureClient(
        source_hosts=(("source-a", "10.0.0.1", 9000), ("source-b", "10.0.0.2", 9000)),
        target_hosts=(("alias-b", "10.0.0.2", 9000), ("target-c", "10.0.0.3", 9000)),
    )
    _public_client(monkeypatch, client)
    with pytest.raises(InvalidSqlInputError, match="Partially overlapping ClickHouse clusters"):
        sql.ch_reconfigure_table(
            "ch",
            "analytics.events",
            ch_engine="MergeTree",
            ch_shard_on_cluster="archive",
            ch_distributed_cluster="archive",
            ch_distributed_on_cluster="core",
            retry_cnt=1,
            timeout_increment=0,
        )
    assert client.commands == []
    assert client.closed
