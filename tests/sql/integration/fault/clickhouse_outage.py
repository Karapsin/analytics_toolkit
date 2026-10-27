from __future__ import annotations

import os
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql.integration._support.identity import resource_name

pytestmark = [pytest.mark.integration, pytest.mark.integration_fault]


def _exercise(alias: str, controller: Any, registry: Any) -> None:
    if os.environ.get("SQL_INTEGRATION_FAULT_GROUP") not in {None, "database"}:
        pytest.skip("database fault group not selected")
    run_id = os.environ["SQL_INTEGRATION_RUN_ID"]
    test_id = os.environ["SQL_INTEGRATION_TEST_ID"]
    label = f"{run_id}.{test_id}.outage"
    table = registry.table(
        alias,
        "integration." + resource_name(run_id, test_id, alias),
        ch_cluster="integration_two_shards",
    )
    controller.stop("clickhouse-replica")
    try:
        with pytest.raises(sql.ClickHouseClusterUnavailableError):
            sql.load_df(
                alias,
                table,
                pd.DataFrame({"id": [1, 2]}),
                table_schema={"id": "Int64"},
                retry_cnt=1,
                query_label=label,
            )
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            result = sql.load_df(
                alias,
                table,
                pd.DataFrame({"id": [1, 2]}),
                table_schema={"id": "Int64"},
                ch_unavailable_shards="healthy_shards",
                retry_cnt=1,
                query_label=label,
                return_metadata=True,
            )
        assert result.inserted_rows == 2
        assert result.metadata.data_complete is False
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            rows = sql.read(
                alias,
                f"SELECT id FROM {table} ORDER BY id",
                ch_unavailable_shards="healthy_shards",
                query_label=label,
            )
        assert rows.id.tolist() == [1, 2]
        gp_source = registry.table("gp", "public." + resource_name(run_id, test_id, alias + "_src"))
        gp_target = registry.table("gp", "public." + resource_name(run_id, test_id, alias + "_dst"))
        sql.load_df(
            "gp",
            gp_source,
            pd.DataFrame({"id": [10, 11]}),
            table_schema={"id": "bigint"},
            query_label=label,
        )
        sql.load_df(
            "gp",
            gp_target,
            pd.DataFrame({"id": [99]}),
            table_schema={"id": "bigint"},
            query_label=label,
        )
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            from_ch = sql.transfer(
                from_db=alias,
                to_db="gp",
                from_sql=f"SELECT id FROM {table}",
                to_table=gp_target,
                write_mode="append",
                ignore_source_staging=False,
                ch_unavailable_shards="healthy_shards",
                return_metadata=True,
                table_schema={"id": "bigint"},
                query_label=label,
            )
        assert from_ch.inserted_rows == 2
        assert from_ch.metadata.row_count_validated is True
        assert from_ch.metadata.source_data_complete is False
        assert from_ch.metadata.target_data_complete is True
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            to_ch = sql.transfer(
                from_db="gp",
                to_db=alias,
                from_sql=f"SELECT id FROM {gp_source}",
                to_table=table,
                write_mode="append",
                ignore_source_staging=True,
                ch_unavailable_shards="healthy_shards",
                return_metadata=True,
                table_schema={"id": "Int64"},
                query_label=label,
            )
        assert to_ch.inserted_rows == 2
        assert to_ch.metadata.row_count_validated is True
        assert to_ch.metadata.source_data_complete is True
        assert to_ch.metadata.target_data_complete is False
    finally:
        controller.restart("clickhouse-replica")
        controller.wait_healthy("clickhouse-replica")
    # Recovery alone must not execute queued schema creation or move data.
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        rows = sql.read(
            alias,
            f"SELECT id FROM {table} ORDER BY id",
            ch_unavailable_shards="healthy_shards",
            query_label=label,
        )
    assert rows.id.tolist() == [1, 2, 10, 11]
    database, relation = table.split(".", 1)
    count = sql.read(
        "ch",
        "SELECT count() FROM clusterAllReplicas('integration_two_shards', system, tables) "
        f"WHERE database = '{database}' AND name = '{relation}_shard'",
        query_label=label,
        output_type="scalar",
    )
    assert count == 1


@pytest.mark.sql_scenario("fault.clickhouse.unavailable_shard.http")
def test_clickhouse_http_new_target_during_shard_outage(
    fault_controller: Any, resource_registry: Any
) -> None:
    _exercise("ch_outage_http", fault_controller, resource_registry)


@pytest.mark.sql_scenario("fault.clickhouse.unavailable_shard.native")
def test_clickhouse_native_new_target_during_shard_outage(
    fault_controller: Any, resource_registry: Any
) -> None:
    _exercise("ch_outage_native", fault_controller, resource_registry)
