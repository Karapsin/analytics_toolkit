"""Coverage follows table topology and explicit intent, never spelling heuristics."""

from __future__ import annotations

import warnings
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.routing import ChClusterRouting, route_sql
from sqlglot import exp, parse_one


def _replicated_table(cluster: Any, database: str, table: str) -> None:
    identity = "12345678-1234-4234-9234-123456789abc"
    for host in ("h1", "h2"):
        cluster.create(
            host,
            f"CREATE TABLE `{database}`.`{table}` UUID '{identity}' (id UInt64) "
            "ENGINE=ReplicatedMergeTree ORDER BY id",
        )
        cluster.tables[(host, f"{database}.{table}")].rows = [(9,)]


@pytest.mark.parametrize(
    ("database", "table"),
    [
        ("System", "records"),
        ("db", "customer__stage__records"),
        ("db", "customer__replace_archive"),
    ],
)
def test_public_reads_use_logical_coverage_for_ordinary_replicated_names(
    outage: Any, database: str, table: str
) -> None:
    cluster, configure = outage
    configure(database=database)
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.tables.clear()
    _replicated_table(cluster, database, table)
    result = sql.read("ch", f"SELECT id FROM `{table}`", retry_cnt=1)
    assert result.id.tolist() == [9]


@pytest.mark.parametrize(
    ("database", "expected"),
    [("system", "clusterAllReplicas"), ("System", "cluster"), ("SYSTEM", "cluster")],
)
def test_routing_plans_preserve_case_distinct_system_database_identity(
    database: str, expected: str
) -> None:
    statement = route_sql(
        f"SELECT id FROM `{database}`.records", routing=ChClusterRouting("core"), database="db"
    )
    tree = parse_one(statement, read=ClickHouseDialect)
    function = next(tree.find_all(exp.Anonymous))
    assert function.name == expected


def test_replica_provisioning_with_complete_logical_input_does_not_mark_partial(
    outage: Any,
) -> None:
    cluster, configure = outage
    configure(cluster_routing=None)
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.tables.clear()
    _replicated_table(cluster, "db", "records")
    cluster.tables.pop(("h2", "db.records"))
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        result = sql.execute(
            "ch",
            "INSERT INTO FUNCTION cluster('core','db','records') VALUES (10)",
            ch_unavailable_shards="healthy_shards",
            return_metadata=True,
            retry_cnt=1,
        )
    assert result.metadata.data_complete is True
    assert not [
        item for item in recorded if issubclass(item.category, sql.ClickHouseDegradedClusterWarning)
    ]


def test_coordinator_facade_preserves_independent_data_on_every_replica_host(
    outage: Any,
) -> None:
    cluster, _configure = outage
    cluster.alive.add("h3")
    cluster.members = [(1, 1, "h2", 9000), (1, 2, "h3", 9000)]
    cluster.tables.clear()
    cluster.create(
        "h1",
        "CREATE TABLE db.front (id UInt64) ENGINE=Distributed('core','db','front_shard',rand())",
    )
    for host, value in [("h2", 20), ("h3", 30)]:
        cluster.create(host, "CREATE TABLE db.front_shard (id UInt64) ENGINE=MergeTree ORDER BY id")
        cluster.tables[(host, "db.front_shard")].rows = [(value,)]
    result = sql.read("ch", "SELECT id FROM db.front", retry_cnt=1)
    assert sorted(result.id.tolist()) == [20, 30]
