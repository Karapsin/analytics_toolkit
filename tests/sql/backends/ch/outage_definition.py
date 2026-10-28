"""Creation preserves schema-only copies and refuses conflicting partial pairs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.outage_ddl import copy_table_definition
from analytics_toolkit.sql.backends.ch.outage_topology import TableObject
from analytics_toolkit.sql.backends.ch.routing import _RoutingClickHouse
from sqlglot import parse_one


@pytest.mark.parametrize("local_source", [False, True])
@pytest.mark.parametrize("explicit_engine", [False, True])
def test_outage_public_table_copy_preserves_structure_without_copying_rows(
    outage: Any, local_source: bool, explicit_engine: bool
) -> None:
    cluster, configure = outage
    if local_source:
        configure(cluster_routing=None, ddl_defaults=None)
    cluster.alive.remove("h2")
    cluster.create(
        "h1",
        "CREATE TABLE db.template (id UInt64 DEFAULT 7) ENGINE=MergeTree "
        "PARTITION BY id ORDER BY id SETTINGS index_granularity=4096",
    )
    cluster.tables[("h1", "db.template")].rows = [(11,)]
    engine = "ENGINE=MergeTree ORDER BY id" if explicit_engine else ""
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        sql.execute(
            "ch",
            f"CREATE TABLE db.schema_copy ON CLUSTER core {engine} AS db.template",
            ch_unavailable_shards="healthy_shards",
        )
    copied = cluster.tables[("h1", "db.schema_copy")]
    assert copied.rows == []
    assert "DEFAULT 7" in copied.ddl
    assert "PARTITION BY id" in copied.ddl
    assert "index_granularity = 4096" in copied.ddl
    assert copied.uuid != cluster.tables[("h1", "db.template")].uuid
    assert ("h2", "db.schema_copy") not in cluster.tables
    assert all(not command.startswith("INSERT") for _, command, _ in cluster.commands)


@pytest.mark.parametrize(
    ("policy", "offline"), [("stop", False), ("healthy_shards", False), ("healthy_shards", True)]
)
def test_outage_partial_pair_cannot_be_classified_as_a_new_dataframe_target(
    outage: Any, policy: str, offline: bool
) -> None:
    cluster, _ = outage
    for host in cluster.hosts:
        del cluster.tables[(host, "db.events")]
    if offline:
        cluster.alive.remove("h2")
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="Partial managed pair"):
        sql.load_df(
            "ch",
            "db.events",
            pd.DataFrame({"id": [9]}),
            table_schema={"id": "UInt64"},
            ch_unavailable_shards=policy,
            retry_cnt=1,
        )
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,)]
    assert cluster.tables[("h2", "db.events_shard")].rows == [(2,)]
    assert not cluster.commands


@pytest.mark.parametrize(
    "properties",
    [
        "ENGINE=ReplacingMergeTree ORDER BY id",
        "ENGINE=MergeTree ORDER BY (id + 1)",
        "ENGINE=MergeTree PARTITION BY id ORDER BY id",
        "ENGINE=MergeTree ORDER BY id SETTINGS index_granularity=4096",
    ],
)
def test_outage_if_not_exists_requires_the_complete_requested_definition(
    outage: Any, properties: str
) -> None:
    cluster, _ = outage
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="conflicts"):
        sql.execute(
            "ch",
            f"CREATE TABLE IF NOT EXISTS db.events_shard ON CLUSTER core (id UInt64) {properties}",
            ch_unavailable_shards="healthy_shards",
        )
    assert not cluster.commands


def test_outage_existing_target_cannot_disappear_before_append_finalization() -> None:
    connection = SimpleNamespace(_ch_outage_client=True, outage_table_exists=lambda _table: False)
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="disappeared"):
        get_backend_adapter("ch").ensure_distributed_target_pair(
            connection,
            "db.events",
            pd.DataFrame({"id": [1]}),
            target_exists=True,
            target_column_types=None,
            insert_column_types=None,
            gp_distributed_by_key=None,
            partition_by=None,
            order_by=None,
            ch_engine="MergeTree",
            ch_cluster="core",
            ch_sharding_key="rand()",
            query_label=None,
            connection_key="ch",
        )


@pytest.mark.parametrize("source_sql", ["SELECT 1", "CREATE TABLE db.source ENGINE=Memory"])
def test_outage_schema_only_copy_refuses_unverified_source_definition(source_sql: str) -> None:
    statement = parse_one("CREATE TABLE db.copy AS db.source", read=_RoutingClickHouse)
    source = TableObject(None, "MergeTree", "MergeTree", source_sql, "")
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="verifiable"):
        copy_table_definition(statement, source)


def test_outage_schema_only_copy_requires_an_existing_source(outage: Any) -> None:
    client = sql.get_sql_connection("ch")
    statement = parse_one("CREATE TABLE db.copy AS db.absent", read=_RoutingClickHouse)
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="source definition"):
        client._expand_create(statement)
