"""Replica routes either prove a usable selection or stop before data submission."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.execution.backend_options import backend_operation


@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("offline", [None, "h1", "h2"])
def test_outage_replica_reads_select_one_usable_host_with_complete_logical_coverage(
    outage: Any, recwarn: Any, policy: str, driver: str, offline: Any
) -> None:
    cluster, configure = outage
    configure(driver=driver, host="h2" if offline == "h1" else "h1")
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.tables.clear()
    cluster.seed(replicated=True)
    if offline is not None:
        cluster.alive.remove(offline)
    result = sql.read(
        "ch",
        "SELECT id FROM db.events",
        ch_unavailable_shards=policy,
        return_metadata=True,
        retry_cnt=1,
        timeout_increment=0,
    )
    chosen = "h2" if offline == "h1" else "h1"
    assert result.data.id.tolist() == ([2] if chosen == "h2" else [1])
    assert cluster.selected_reads == [(chosen, "db.events_shard")]
    assert result.metadata.data_complete is True
    physical = next(
        item for item in result.metadata.ch_cluster_status if item["table"] == "db.events_shard"
    )
    assert physical["complete"] is True
    assert physical["host_coverage_complete"] is (offline is None)
    assert not recwarn


@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("missing", ["h1", "h2"])
def test_outage_reachable_table_missing_replica_never_gets_a_data_query(
    outage: Any, policy: str, driver: str, missing: str
) -> None:
    cluster, configure = outage
    configure(driver=driver)
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.tables.clear()
    cluster.seed(replicated=True)
    del cluster.tables[(missing, "db.events_shard")]
    warning = (
        pytest.warns(sql.ClickHouseDegradedClusterWarning)
        if policy == "healthy_shards"
        else nullcontext()
    )
    with backend_operation(policy) as state:
        with warning, pytest.raises(
            sql.ClickHouseClusterUnavailableError, match="table-ready replicas"
        ):
            sql.read(
                "ch",
                "SELECT id FROM db.events",
                return_metadata=True,
                retry_cnt=1,
                timeout_increment=0,
            )
        assert not state.alias_complete("ch")
        physical = next(
            item for item in state.statuses.values() if item["table"] == "db.events_shard"
        )
        assert physical["complete"] is False
        assert len(physical["selected_hosts"]) == 1
    assert cluster.selected_reads == []
    assert cluster.commands == []


@pytest.mark.parametrize("driver", ["http", "native"])
def test_outage_table_missing_whole_replicated_shard_remains_an_ad_hoc_partial_read(
    outage: Any, driver: str
) -> None:
    cluster, configure = outage
    configure(driver=driver)
    cluster.tables.clear()
    cluster.seed(replicated=True)
    for table in ("db.events", "db.events_shard"):
        del cluster.tables[("h2", table)]
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        result = sql.read(
            "ch",
            "SELECT id FROM db.events",
            ch_unavailable_shards="healthy_shards",
            return_metadata=True,
            retry_cnt=1,
            timeout_increment=0,
        )
    assert result.data.id.tolist() == [1]
    assert result.metadata.data_complete is False
    assert cluster.selected_reads == [("h1", "db.events_shard")]
    assert ("h2", "db.events_shard") not in cluster.tables
    assert not cluster.commands
    with pytest.raises(sql.ClickHouseClusterUnavailableError):
        sql.read("ch", "SELECT id FROM db.events", retry_cnt=1, timeout_increment=0)
    assert cluster.selected_reads == [("h1", "db.events_shard")]
