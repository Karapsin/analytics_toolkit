"""A facade outage cannot prevent deployment on a healthy physical cluster."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql.integration._support.identity import query_label, resource_name
from tests.sql.integration.manifest import scenario_param

if TYPE_CHECKING:
    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_fault]
_PHYSICAL_CLUSTER = "integration_cluster"
_FACADE_CLUSTER = "integration_archive_cluster"


@pytest.mark.parametrize(
    "alias",
    [
        scenario_param(f"fault.clickhouse.optional_facade.{alias}", alias)
        for alias in ("ch", "ch_native")
    ],
)
def test_healthy_physical_cluster_survives_optional_facade_outage(
    fault_controller: Any, resource_registry: ResourceRegistry, alias: str
) -> None:
    if os.environ.get("SQL_INTEGRATION_FAULT_GROUP") not in {None, "database"}:
        pytest.skip("database fault group not selected")
    run_id = os.environ["SQL_INTEGRATION_RUN_ID"]
    test_id = os.environ["SQL_INTEGRATION_TEST_ID"]
    label = query_label(run_id, test_id, "optional_facade")
    relation = resource_name(run_id, test_id, alias, limit=44)
    tables = {
        kind: "integration." + relation + "_" + kind
        for kind in ("schema", "load", "query", "transfer")
    }
    for target in tables.values():
        resource_registry.table(alias, target, ch_cluster=_PHYSICAL_CLUSTER)
        resource_registry.table(alias, target, ch_cluster=_FACADE_CLUSTER)
    table = tables["schema"]
    placement = {
        "ch_shard_on_cluster": _PHYSICAL_CLUSTER,
        "ch_distributed_on_cluster": _FACADE_CLUSTER,
        "ch_distributed_cluster": _PHYSICAL_CLUSTER,
        "ch_sharding_key": "rand()",
    }

    fault_controller.stop("clickhouse-replica")
    try:
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            sql.create_table(
                alias,
                table,
                table_schema={"id": "UInt64"},
                **placement,
                order_by="id",
                retry_cnt=1,
                query_label=label,
            )
        sql.execute(
            alias,
            f"INSERT INTO {table}_shard VALUES (17), (19)",
            retry_cnt=1,
            query_label=label,
        )
        rows = sql.read(
            alias,
            f"SELECT id FROM {table}_shard ORDER BY id",
            retry_cnt=1,
            query_label=label,
        )
        assert rows.id.tolist() == [17, 19]
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            assert (
                sql.load_df(
                    alias,
                    tables["load"],
                    pd.DataFrame({"id": [17, 19]}),
                    table_schema={"id": "UInt64"},
                    **placement,
                    retry_cnt=1,
                    query_label=label,
                )
                == 2
            )
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            sql.create_table(
                alias,
                tables["query"],
                sql=f"SELECT id FROM {table}_shard",
                **placement,
                retry_cnt=1,
                query_label=label,
            )
        with pytest.warns(sql.ClickHouseDegradedClusterWarning):
            assert (
                sql.transfer(
                    from_db="ch_source",
                    to_db=alias,
                    from_sql=f"SELECT id FROM {table}_shard",
                    to_table=tables["transfer"],
                    key_columns=["id"],
                    table_schema={"id": "UInt64"},
                    **placement,
                    retry_cnt=1,
                    full_retry_cnt=1,
                    query_label=label,
                )
                == 2
            )
        for target in tables.values():
            actual = sql.read(
                alias,
                f"SELECT id FROM {target}_shard ORDER BY id",
                retry_cnt=1,
                query_label=label,
            )
            assert actual.id.tolist() == [17, 19]
    finally:
        fault_controller.restart("clickhouse-replica")
        fault_controller.wait_healthy("clickhouse-replica")

    # The excluded host must not receive a previously queued facade CREATE.
    count = sql.read(
        alias,
        f"SELECT count() FROM clusterAllReplicas('{_FACADE_CLUSTER}', system, tables) "
        f"WHERE database = 'integration' AND startsWith(name, '{relation}_')",
        output_type="scalar",
        retry_cnt=1,
        query_label=label,
    )
    assert count == 0
