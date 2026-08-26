from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql

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
