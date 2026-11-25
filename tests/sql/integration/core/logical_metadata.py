"""Disposable native metadata proves logical totals and shared replica leases."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from analytics_toolkit import sql

from tests.sql.integration._support.reconfiguration import (
    create_owned,
    label,
    literal,
    quoted,
    resource_name,
)

if TYPE_CHECKING:
    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]
_CLUSTER = "integration_replicated_cluster"
_DATABASE = "integration"


@pytest.mark.sql_scenario("audit.metadata.clickhouse.logical_totals")
def test_native_clickhouse_logical_statistics_and_replica_identity(
    resource_registry: ResourceRegistry,
) -> None:
    for alias in ("ch_routed_replicas", "ch_routed_replicas_native"):
        for replicated in (False, True):
            relation = resource_name(f"metadata_{alias}_{replicated}")
            physical = relation + "_shard"
            table, shard = quoted(_DATABASE, relation), quoted(_DATABASE, physical)
            engine = (
                f"ReplicatedMergeTree({literal('/clickhouse/audit/' + relation)}, '{{replica}}')"
                if replicated
                else "MergeTree"
            )
            create_owned(
                resource_registry,
                f"CREATE TABLE {shard} ON CLUSTER {quoted(_CLUSTER)} "
                f"(id UInt64) ENGINE={engine} ORDER BY id",
                f"DROP TABLE IF EXISTS {shard} ON CLUSTER {quoted(_CLUSTER)} SYNC",
            )
            create_owned(
                resource_registry,
                f"CREATE TABLE {table} (id UInt64) ENGINE=Distributed("
                f"{literal(_CLUSTER)}, {literal(_DATABASE)}, {literal(physical)}, rand())",
                f"DROP TABLE IF EXISTS {table} SYNC",
            )
            function = "cluster" if replicated else "clusterAllReplicas"
            sql.execute(
                "ch",
                f"INSERT INTO FUNCTION {function}({literal(_CLUSTER)}, "
                f"{literal(_DATABASE)}, {literal(physical)}) VALUES (1), (2), (3)",
                retry_cnt=1,
                query_label=label("metadata-owned-seed"),
            )
            if replicated:
                sql.execute(
                    "ch",
                    f"SYSTEM SYNC REPLICA ON CLUSTER {quoted(_CLUSTER)} {shard}",
                    retry_cnt=1,
                    query_label=label("metadata-replication-barrier"),
                )
            expected = 3 if replicated else 6
            actual = sql.read(alias, f"SELECT id FROM {shard}", retry_cnt=1)
            assert len(actual) == expected
            direct = sql.show_tables(alias, schema=_DATABASE, table_name=physical)
            assert direct.row_count.tolist() == [expected]
            assert direct.attrs.get("data_complete", True) is True
            facade = sql.show_tables(
                "ch",
                schema=_DATABASE,
                table_name=relation,
                ch_distributed_table_stats=True,
                conditions=f"row_count = {expected}",
            )
            assert facade.row_count.tolist() == [expected]
            assert facade.attrs.get("data_complete", True) is True
            excluded = sql.show_tables(
                "ch",
                schema=_DATABASE,
                table_name=relation,
                ch_distributed_table_stats=True,
                conditions=f"row_count > {expected}",
            )
            assert excluded.empty
        # Both hosts remain reachable, but independent data exists on only
        # the coordinator. Its estimate cannot claim complete physical scope.
        partial = resource_name(f"metadata_partial_{alias}")
        partial_table = quoted(_DATABASE, partial)
        create_owned(
            resource_registry,
            f"CREATE TABLE {partial_table} (id UInt64) ENGINE=MergeTree ORDER BY id",
            f"DROP TABLE IF EXISTS {partial_table} SYNC",
        )
        sql.execute(
            "ch",
            f"INSERT INTO {partial_table} VALUES (1), (2), (3)",
            retry_cnt=1,
            query_label=label("metadata-partial-owned-seed"),
        )
        incomplete = sql.show_tables(alias, schema=_DATABASE, table_name=partial)
        assert incomplete.row_count.tolist() == [3]
        assert incomplete.attrs.get("data_complete") is False
