"""Unsupported explicit CREATE AS schemas fail before their definition can be lost."""

from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit import sql


@pytest.mark.parametrize(
    "schema",
    [
        "id String",
        "renamed UInt64",
        "id UInt64 DEFAULT 7",
        "id UInt64, CONSTRAINT positive CHECK id > 0",
        "id UInt64, INDEX by_id id TYPE minmax GRANULARITY 1",
    ],
)
@pytest.mark.parametrize("source", ["SELECT id FROM db.events", "db.events_shard"])
def test_outage_explicit_create_as_schema_is_never_replaced_or_mutated(
    outage: Any, schema: str, source: str
) -> None:
    cluster, _ = outage
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="explicit schema"):
        sql.execute(
            "ch",
            f"CREATE TABLE db.copy ON CLUSTER core ({schema}) "
            f"ENGINE=MergeTree ORDER BY tuple() AS {source}",
            ch_unavailable_shards="healthy_shards",
        )
    assert not cluster.commands
    assert all(name != "db.copy" for _host, name in cluster.tables)
    assert cluster.tables[("h1", "db.events_shard")].rows == [(1,)]
    assert cluster.tables[("h2", "db.events_shard")].rows == [(2,)]
