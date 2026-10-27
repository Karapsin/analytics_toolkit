from __future__ import annotations

import os

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.metadata import build_ch_completion_query

from tests.sql.integration._support.identity import query_label

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


@pytest.mark.sql_scenario("clickhouse.completion.literal_prefixes")
def test_clickhouse_completion_prefixes_are_literal() -> None:
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
    assert "tables" in names(None, "TaB")
    assert names("missing'\\namespace", "") == []
