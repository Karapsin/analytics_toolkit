from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.dml.transfer.flow.stage_identity import (
    resolve_destination_identity,
)

from tests.sql.integration._support.identity import resource_name

if TYPE_CHECKING:
    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _table(suffix: str) -> str:
    run_id = os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8])
    test_id = os.environ.get("SQL_INTEGRATION_TEST_ID", "manual")
    return f"integration.{resource_name(run_id, test_id, suffix)}"


@pytest.mark.sql_scenario("clickhouse.cluster_routing.transports")
def test_clickhouse_cluster_routing_http_and_native(
    resource_registry: ResourceRegistry,
) -> None:
    for alias in ("ch_routed", "ch_routed_native"):
        table = resource_registry.table(
            "ch",
            _table(alias),
            ch_cluster="integration_cluster",
        )
        assert (
            sql.load_df(
                alias,
                table,
                pd.DataFrame({"id": [3, 4]}),
                write_mode="replace",
                order_by="id",
                ch_only_shard=True,
            )
            == 2
        )
        sql.execute(alias, f"INSERT INTO {table} VALUES (1), (2)")

        rows = sql.read(alias, f"SELECT id FROM {table} ORDER BY id")
        assert rows["id"].tolist() == [1, 2, 3, 4]


@pytest.mark.sql_scenario("clickhouse.cluster_routing.transfer_replicas")
def test_clickhouse_cluster_routed_transfer_across_replicas(
    resource_registry: ResourceRegistry,
) -> None:
    source = resource_registry.table(
        "trino_values",
        f"iceberg.{_table('replica_source')}",
    )
    expected = pd.DataFrame(
        {
            "id": list(range(12)),
            "payload": [f"row-{index}" for index in range(12)],
        }
    )
    assert sql.load_df("trino_values", source, expected, write_mode="replace") == 12

    for alias in ("ch_routed_replicas", "ch_routed_replicas_native"):
        target = resource_registry.table(
            alias,
            _table(alias),
            ch_cluster="integration_replicated_cluster",
        )
        assert (
            sql.transfer(
                "trino_values",
                alias,
                from_table=source,
                to_table=target,
                write_mode="replace",
                batch_size=2,
                adaptive_batch_size=False,
                target_rows_per_second=False,
                concurrency=3,
                retry_cnt=1,
                full_retry_cnt=1,
                table_schema={"id": "Int64", "payload": "String"},
            )
            == 12
        )

        actual = sql.read(alias, f"SELECT id, payload FROM {target} ORDER BY id")
        assert actual.to_dict("records") == expected.to_dict("records")

        reverse_target = resource_registry.table(
            "trino_values",
            f"iceberg.{_table(f'{alias}_reverse')}",
        )
        assert (
            sql.transfer(
                alias,
                "trino_values",
                from_table=target,
                to_table=reverse_target,
                write_mode="replace",
                batch_size=2,
                adaptive_batch_size=False,
                target_rows_per_second=False,
                transfer_keys="id",
                transfer_key_values=list(range(12)),
                read_concurrency=3,
                write_concurrency=2,
                retry_cnt=1,
                full_retry_cnt=1,
            )
            == 12
        )
        reverse_actual = sql.read(
            "trino_values",
            f"SELECT id, payload FROM {reverse_target} ORDER BY id",
        )
        assert reverse_actual.to_dict("records") == expected.to_dict("records")

        stage_prefix = resolve_destination_identity(target, "ch").hash_prefix
        source_stage_prefix = resolve_destination_identity(reverse_target, "trino").hash_prefix
        remaining = sql.read(
            alias,
            "SELECT count() AS stage_count "
            "FROM clusterAllReplicas("
            "'integration_replicated_cluster', system, tables) "
            "WHERE database = 'integration' "
            f"AND (name LIKE '{stage_prefix}__%' "
            f"OR name LIKE '{source_stage_prefix}__%')",
        )
        assert int(remaining.iloc[0, 0]) == 0
