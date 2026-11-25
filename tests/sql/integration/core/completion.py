from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.metadata import build_ch_completion_query

from tests.sql.integration._support.backends import integration_table
from tests.sql.integration._support.identity import query_label

if TYPE_CHECKING:
    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


@pytest.mark.sql_scenario("clickhouse.completion.literal_prefixes")
def test_clickhouse_completion_prefixes_are_literal(resource_registry: ResourceRegistry) -> None:
    # The managed profile runs the ch alias with both HTTP and native transports.
    label = query_label(
        os.environ["SQL_INTEGRATION_RUN_ID"],
        os.environ["SQL_INTEGRATION_TEST_ID"],
        "completion",
    )

    def names(schema: str | None, prefix: str) -> list[str]:
        frame = sql.read(
            "ch", build_ch_completion_query(schema, prefix), query_label=label, retry_cnt=1
        )
        return [] if frame.empty else frame.iloc[:, 0].tolist()

    all_names = names("system", "")
    assert "tables" in all_names
    for prefix in ("TaB", "query_", "%", "_", "!", "a'b", "a\\n\\", "x'); --"):
        assert names("system", prefix) == [
            name for name in all_names if name.lower().startswith(prefix.lower())
        ]
    table = integration_table("ch", "completion_current_database")
    acknowledged = False

    def cleanup() -> None:
        if acknowledged:
            sql.execute("ch", f"DROP TABLE IF EXISTS {table}", retry_cnt=1, query_label=label)

    resource_registry.finalizer(cleanup)
    sql.execute(
        "ch", f"CREATE TABLE {table} (id Int64) ENGINE=Memory", retry_cnt=1, query_label=label
    )
    acknowledged = True
    database, local_name = table.split(".", 1)
    current_names = names(database, "")
    assert local_name in current_names
    assert names(None, "") == current_names
    assert names(None, "TaB") == [name for name in current_names if name.lower().startswith("tab")]
    assert names("system", local_name) == []
    assert names("missing'\\namespace", "") == []
