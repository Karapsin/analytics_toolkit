"""DDL defaults never broaden local SQL or physical-table delivery."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql


@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
@pytest.mark.parametrize("driver", ["http", "native"])
def test_outage_defaults_preserve_local_ddl_and_all_physical_insert_paths(
    outage: Any, policy: str, driver: str
) -> None:
    cluster, configure = outage
    configure(cluster_routing=None, driver=driver)
    cluster.alive.remove("h2")
    client = sql.get_sql_connection("ch", ch_unavailable_shards=policy)
    client.command("INSERT INTO db.events_shard VALUES (3)")
    client.insert("db.events_shard", [(4,)], column_names=["id"])
    client.insert_df("db.events_shard", pd.DataFrame({"id": [5]}))
    client.command("INSERT INTO db.events_shard SELECT 6")
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,), (3,), (4,), (5,), (6,)]
    assert cluster.tables[("h2", "db.events_shard")].rows == [(2,)]
    client.command("CREATE TABLE db.only_here (id UInt64) ENGINE=MergeTree ORDER BY id")
    assert ("h1", "db.only_here") in cluster.tables
    assert ("h2", "db.only_here") not in cluster.tables
    client.command("DROP TABLE db.events_shard")
    assert ("h1", "db.events_shard") not in cluster.tables
    assert ("h2", "db.events_shard") in cluster.tables
    assert all(
        "ON CLUSTER" not in command and "cluster(" not in command
        for _, command, _ in cluster.commands
    )
    assert all("insert_shard_id" not in settings for _, _, settings in cluster.commands)


@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
def test_outage_explicit_cluster_scope_still_applies_without_routing_override(
    outage: Any, policy: str
) -> None:
    cluster, configure = outage
    configure(cluster_routing=None)
    cluster.alive.remove("h2")
    client = sql.get_sql_connection("ch", ch_unavailable_shards=policy)
    with pytest.raises(sql.ClickHouseClusterUnavailableError):
        client.command("DROP TABLE db.events_shard ON CLUSTER core")
    assert not cluster.commands


@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
@pytest.mark.parametrize("source", ["db.events", "cluster('core', 'db', 'events_shard')"])
def test_outage_global_source_does_not_turn_local_insert_target_into_cluster_target(
    outage: Any, policy: str, source: str
) -> None:
    cluster, configure = outage
    configure(cluster_routing=None)
    cluster.alive.remove("h2")
    cluster.create("h1", "CREATE TABLE db.local_target (id UInt64) ENGINE=MergeTree ORDER BY id")
    if policy == "stop":
        with pytest.raises(sql.ClickHouseClusterUnavailableError):
            sql.execute("ch", f"INSERT INTO db.local_target SELECT id FROM {source}")
        assert not cluster.commands
    else:
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            sql.execute(
                "ch",
                f"INSERT INTO db.local_target SELECT id FROM {source}",
                ch_unavailable_shards=policy,
            )
        assert cluster.tables[("h1", "db.local_target")].rows == [(1,)]
        assert ("h2", "db.local_target") not in cluster.tables
        assert cluster.commands[-1][1] == "INSERT INTO db.local_target"


def test_outage_partial_local_ctas_is_rejected_before_creation(outage: Any) -> None:
    cluster, configure = outage
    configure(cluster_routing=None)
    cluster.alive.remove("h2")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        sql.ClickHouseClusterTopologyError, match="separate CREATE"
    ):
        sql.execute(
            "ch",
            "CREATE TABLE db.local_copy ENGINE=MergeTree ORDER BY id AS SELECT id FROM db.events",
            ch_unavailable_shards="healthy_shards",
        )
    assert not cluster.commands
