"""Physical replicas may differ only in the native replica-name engine argument."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import (
    ClickHouseClusterTopologyError,
    InvalidSqlInputError,
)
from analytics_toolkit.sql.execution.plans import SqlPlan


def _literal_replica_tables(cluster):
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    for host in cluster.hosts:
        original = cluster.tables.pop((host, "db.events_shard"))
        cluster.create(
            host,
            f"CREATE TABLE db.events_shard UUID '{original.uuid}' (id UInt64) "
            f"ENGINE=ReplicatedMergeTree('/tables/{{uuid}}/1', '{host}') ORDER BY id",
        )
        cluster.tables[(host, "db.events_shard")].rows = deepcopy(original.rows)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_public_reconfiguration_accepts_distinct_literal_replica_names(outage, driver):
    cluster, configure = outage
    configure(driver=driver)
    _literal_replica_tables(cluster)
    original = deepcopy(cluster.tables)

    plan = sql.ch_reconfigure_table(
        "ch",
        "db.events",
        ch_settings={"index_granularity": 4096},
        dry_run=True,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert isinstance(plan, SqlPlan)
    assert plan.options["strategy"] == "settings"
    assert any("index_granularity" in statement.sql for statement in plan.statements)
    assert cluster.tables == original
    assert cluster.commands == []


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("difference", ["path", "default"])
def test_public_reconfiguration_rejects_other_replica_definition_differences(
    outage, driver, difference
):
    cluster, configure = outage
    configure(driver=driver)
    _literal_replica_tables(cluster)
    changed = cluster.tables.pop(("h2", "db.events_shard"))
    columns = "id UInt64 DEFAULT 7" if difference == "default" else "id UInt64"
    path = "/other/{uuid}/1" if difference == "path" else "/tables/{uuid}/1"
    cluster.create(
        "h2",
        f"CREATE TABLE db.events_shard UUID '{changed.uuid}' ({columns}) "
        f"ENGINE=ReplicatedMergeTree('{path}', 'h2') ORDER BY id",
    )
    cluster.tables[("h2", "db.events_shard")].rows = deepcopy(changed.rows)
    original = deepcopy(cluster.tables)

    with pytest.raises((ClickHouseClusterTopologyError, InvalidSqlInputError)):
        sql.ch_reconfigure_table(
            "ch",
            "db.events",
            ch_settings={"index_granularity": 4096},
            dry_run=True,
            retry_cnt=1,
            timeout_increment=0,
        )

    assert cluster.tables == original
    assert cluster.commands == []
