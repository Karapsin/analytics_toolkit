"""Recovered hosts permit explicit idempotent cleanup of partially created pairs."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
@pytest.mark.parametrize("api", ["drop_tables", "execute"])
def test_outage_recovered_hosts_allow_drop_if_exists_without_schema_provisioning(
    outage: Any, driver: str, policy: str, api: str
) -> None:
    cluster, configure = outage
    configure(driver=driver)
    cluster.alive.remove("h2")
    table = "db.outage_cleanup"
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        sql.load_df(
            "ch",
            table,
            pd.DataFrame({"id": [9]}),
            table_schema={"id": "UInt64"},
            ch_unavailable_shards="healthy_shards",
            retry_cnt=1,
            timeout_increment=0,
        )
    assert ("h2", table) not in cluster.tables
    assert ("h2", table + "_shard") not in cluster.tables
    cluster.alive.add("h2")
    before = len(cluster.commands)
    if api == "drop_tables":
        sql.drop_tables(
            "ch", table, if_exists=True, ch_cluster="core", ch_unavailable_shards=policy
        )
    else:
        sql.execute(
            "ch",
            [f"DROP TABLE IF EXISTS {name} ON CLUSTER core" for name in (table, table + "_shard")],
            ch_unavailable_shards=policy,
            retry_cnt=1,
            timeout_increment=0,
        )
    assert not any(name in {table, table + "_shard"} for _host, name in cluster.tables)
    assert not any("CREATE" in query for _host, query, _settings in cluster.commands[before:])
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,)]


@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
@pytest.mark.parametrize("recovered", [False, True])
def test_outage_cleanup_still_requires_reachable_hosts_and_explicit_if_exists(
    outage: Any, policy: str, recovered: bool
) -> None:
    cluster, _configure = outage
    cluster.alive.remove("h2")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        sql.load_df(
            "ch",
            "db.retained_cleanup",
            pd.DataFrame({"id": [9]}),
            table_schema={"id": "UInt64"},
            ch_unavailable_shards="healthy_shards",
            retry_cnt=1,
            timeout_increment=0,
        )
    if recovered:
        cluster.alive.add("h2")
    before = len(cluster.commands)
    warning = (
        pytest.warns(sql.ClickHouseDegradedClusterWarning)
        if policy == "healthy_shards"
        else nullcontext()
    )
    with warning, pytest.raises(sql.ClickHouseClusterUnavailableError):
        sql.drop_tables(
            "ch",
            "db.retained_cleanup",
            if_exists=not recovered,
            ch_cluster="core",
            ch_unavailable_shards=policy,
        )
    assert len(cluster.commands) == before
    assert cluster.tables[("h1", "db.retained_cleanup_shard")].rows == [(9,)]
