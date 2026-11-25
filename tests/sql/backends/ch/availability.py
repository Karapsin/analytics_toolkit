"""Physical storage availability is required; other deployments are advisory."""

from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit import sql


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("edge_hosts", [("h1", "h3"), ("h3", "h4")])
def test_default_creation_requires_shards_and_warns_for_optional_facade(
    outage: Any, driver: str, edge_hosts: tuple[str, str]
) -> None:
    cluster, configure = outage
    configure(driver=driver)
    cluster.members_by_cluster["edge"] = [
        (position, 1, host, 9000) for position, host in enumerate(edge_hosts, 1)
    ]
    with pytest.warns(sql.ClickHouseDegradedClusterWarning, match="not required.*storage"):
        result = sql.create_table(
            "ch",
            "db.optional_facade",
            table_schema={"id": "UInt64"},
            ch_shard_on_cluster="core",
            ch_distributed_on_cluster="edge",
            ch_distributed_cluster="core",
            ch_ddl_ready_timeout_seconds=0.1,
            retry_cnt=1,
            return_metadata=True,
        )
    assert result.metadata.data_complete is True
    assert all((host, "db.optional_facade_shard") in cluster.tables for host in ("h1", "h2"))
    assert not any(host in {"h3", "h4"} for host, _query, _settings in cluster.commands)
    assert not any("ON CLUSTER edge" in query for _host, query, _settings in cluster.commands)
    optional = [item for item in result.metadata.ch_cluster_status if item["cluster"] == "edge"]
    assert optional
    assert all(item["availability_required"] is False for item in optional)
    assert all(item["host_coverage_complete"] is False for item in optional)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_missing_required_shard_stops_before_optional_facade_creation(
    outage: Any, driver: str
) -> None:
    cluster, configure = outage
    configure(driver=driver)
    cluster.alive.remove("h2")
    cluster.members_by_cluster["edge"] = [(1, 1, "h1", 9000), (2, 1, "h3", 9000)]
    with pytest.raises(sql.ClickHouseClusterUnavailableError, match="core"):
        sql.create_table(
            "ch",
            "db.required",
            table_schema={"id": "UInt64"},
            ch_shard_on_cluster="core",
            ch_distributed_on_cluster="edge",
            ch_distributed_cluster="core",
            ch_ddl_ready_timeout_seconds=0.1,
            retry_cnt=1,
        )
    assert not cluster.commands


@pytest.mark.parametrize("wait_policy", ["wait_all", "wait_distr"])
@pytest.mark.parametrize("configured", [False, True])
def test_explicit_facade_wait_still_requires_facade_availability(
    outage: Any, wait_policy: str, configured: bool
) -> None:
    cluster, configure = outage
    if configured:
        configure(ch_ddl_wait_policy=wait_policy)
    cluster.members_by_cluster["edge"] = [(1, 1, "h1", 9000), (2, 1, "h3", 9000)]
    with pytest.raises(sql.ClickHouseClusterUnavailableError, match="edge"):
        sql.create_table(
            "ch",
            "db.explicit_wait",
            table_schema={"id": "UInt64"},
            ch_shard_on_cluster="core",
            ch_distributed_on_cluster="edge",
            ch_distributed_cluster="core",
            ch_ddl_wait_policy=None if configured else wait_policy,
            retry_cnt=1,
        )
    assert not cluster.commands


def test_no_shard_cluster_preserves_full_participating_cluster_requirement(outage: Any) -> None:
    cluster, configure = outage
    configure(
        ddl_defaults={
            "regular": {
                "create_distributed_pair": True,
                "shard": {"engine": "MergeTree", "on_cluster": None},
                "distributed": {
                    "engine_template": (
                        "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                    ),
                    "cluster": "core",
                    "on_cluster": "edge",
                    "sharding_key": "rand()",
                },
            },
        }
    )
    cluster.members_by_cluster["edge"] = [(1, 1, "h1", 9000), (2, 1, "h3", 9000)]
    with pytest.raises(sql.ClickHouseClusterUnavailableError, match="edge"):
        sql.create_table(
            "ch",
            "db.local_physical",
            table_schema={"id": "UInt64"},
            ch_shard_on_cluster=None,
            ch_distributed_on_cluster="edge",
            ch_distributed_cluster="core",
            retry_cnt=1,
        )
    assert not cluster.commands


def test_cluster_alias_with_same_endpoints_is_required_storage(outage: Any) -> None:
    cluster, _configure = outage
    cluster.members_by_cluster["alias"] = list(cluster.members)
    cluster.alive.remove("h2")
    with pytest.raises(sql.ClickHouseClusterUnavailableError, match="alias"):
        sql.execute(
            "ch",
            "CREATE TABLE db.alias_test ON CLUSTER alias (id UInt64) ENGINE=MergeTree ORDER BY id",
            retry_cnt=1,
        )
    assert not cluster.commands


def test_missing_optional_facade_replica_does_not_reduce_physical_read_coverage(
    outage: Any,
) -> None:
    cluster, configure = outage
    configure(cluster_routing={"cluster": "edge"})
    cluster.members_by_cluster["edge"] = [(1, 1, "h1", 9000), (2, 1, "h3", 9000)]
    with pytest.warns(sql.ClickHouseDegradedClusterWarning, match="not required.*storage"):
        result = sql.read("ch", "SELECT id FROM db.events", return_metadata=True, retry_cnt=1)
    assert result.data.id.tolist() == [1, 2]
    assert result.metadata.data_complete is True


def test_optional_cluster_does_not_waive_actual_source_data_coverage(outage: Any) -> None:
    cluster, _configure = outage
    cluster.members_by_cluster["edge"] = [(1, 1, "h1", 9000), (2, 1, "h3", 9000)]
    with pytest.raises(sql.ClickHouseClusterUnavailableError):
        sql.read("ch", "SELECT id FROM cluster('edge', 'db', 'events_shard')", retry_cnt=1)


def test_optional_facade_does_not_waive_destructive_execution_hosts(outage: Any) -> None:
    cluster, _configure = outage
    cluster.members_by_cluster["edge"] = [(1, 1, "h1", 9000), (2, 1, "h3", 9000)]
    with pytest.raises(sql.ClickHouseClusterUnavailableError, match="edge"):
        sql.execute("ch", "DROP TABLE db.events ON CLUSTER edge", retry_cnt=1)
    assert not cluster.commands


@pytest.mark.parametrize("populated", [False, True])
@pytest.mark.parametrize("edge_hosts", [("h1", "h3"), ("h3", "h4")])
def test_explicit_physical_create_outside_configured_shards_still_requires_all_hosts(
    outage: Any, populated: bool, edge_hosts: tuple[str, str]
) -> None:
    cluster, _configure = outage
    cluster.members_by_cluster["edge"] = [
        (position, 1, host, 9000) for position, host in enumerate(edge_hosts, 1)
    ]
    populate = " AS SELECT toUInt64(42) AS id" if populated else ""
    with pytest.raises(sql.ClickHouseClusterUnavailableError, match="edge"):
        sql.execute(
            "ch",
            "CREATE TABLE db.new_physical ON CLUSTER edge (id UInt64) "
            f"ENGINE=MergeTree ORDER BY id{populate}",
            retry_cnt=1,
        )
    assert not cluster.commands


def test_healthy_optional_cluster_keeps_normal_cluster_create_behavior(outage: Any) -> None:
    cluster, _configure = outage
    cluster.members_by_cluster["edge"] = [(1, 1, "h1", 9000), (2, 1, "h2", 9000)]
    cluster.database_engine = "Replicated"
    sql.create_table(
        "ch",
        "db.full_facade",
        table_schema={"id": "UInt64"},
        ch_shard_on_cluster="core",
        ch_distributed_on_cluster="edge",
        ch_distributed_cluster="core",
        ch_ddl_ready_timeout_seconds=0.1,
        retry_cnt=1,
    )
    assert any("ON CLUSTER edge" in query for _host, query, _settings in cluster.commands)


def test_unset_physical_cluster_does_not_make_raw_facade_deployment_optional(outage: Any) -> None:
    cluster, configure = outage
    configure(ddl_defaults={"regular": {"shard": {"engine": "MergeTree"}}})
    cluster.members_by_cluster["edge"] = [(1, 1, "h1", 9000), (2, 1, "h3", 9000)]
    with pytest.raises(sql.ClickHouseClusterUnavailableError, match="edge"):
        sql.execute(
            "ch",
            "CREATE TABLE db.raw_facade ON CLUSTER edge (id UInt64) "
            "ENGINE=Distributed('core', 'db', 'events_shard', rand())",
            retry_cnt=1,
        )
    assert not cluster.commands


def test_required_shard_cluster_macro_is_resolved_before_optional_deployment(outage: Any) -> None:
    cluster, configure = outage
    configure(ddl_defaults={"regular": {"shard": {"on_cluster": "{cluster}"}}})
    cluster.members_by_cluster["edge"] = [(1, 1, "h1", 9000), (2, 1, "h3", 9000)]
    with pytest.warns(sql.ClickHouseDegradedClusterWarning, match="not required.*storage"):
        sql.execute(
            "ch",
            "CREATE TABLE db.macro_facade ON CLUSTER edge (id UInt64) "
            "ENGINE=Distributed('core', 'db', 'events_shard', rand())",
            retry_cnt=1,
        )
    assert ("h1", "db.macro_facade") in cluster.tables
    assert not any("ON CLUSTER edge" in query for _host, query, _settings in cluster.commands)


def test_unverifiable_required_cluster_cannot_authorize_optional_deployment(outage: Any) -> None:
    cluster, configure = outage
    configure(ddl_defaults={"regular": {"shard": {"on_cluster": "missing"}}})
    cluster.members_by_cluster["missing"] = []
    cluster.members_by_cluster["edge"] = [(1, 1, "h1", 9000), (2, 1, "h3", 9000)]
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="Required shard cluster"):
        sql.execute(
            "ch",
            "CREATE TABLE db.unverifiable ON CLUSTER edge (id UInt64) "
            "ENGINE=Distributed('core', 'db', 'events_shard', rand())",
            retry_cnt=1,
        )
    assert not cluster.commands
