"""Read coverage does not confer ownership of a facade's physical destination."""

from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit import sql


def _unowned_facade(
    outage: Any, *, routed: bool, mismatch: bool = False, physical: str = "storage"
) -> Any:
    cluster, configure = outage
    configure(cluster_routing={"cluster": "core"} if routed else None)
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.tables.clear()
    for host, value in [("h1", 20), ("h2", 30)]:
        cluster.create(host, f"CREATE TABLE db.{physical} (id UInt64) ENGINE=MergeTree ORDER BY id")
        cluster.tables[(host, f"db.{physical}")].rows = [(value,)]
        columns = "id UInt64, extra String" if mismatch else "id UInt64"
        cluster.create(
            host,
            f"CREATE TABLE db.front ({columns}) "
            f"ENGINE=Distributed('core','db','{physical}',rand())",
        )
    return cluster


def test_automatic_read_includes_independent_data_behind_unowned_facade(outage: Any) -> None:
    cluster = _unowned_facade(outage, routed=True)
    original = {
        key: (table.uuid, table.ddl, list(table.rows)) for key, table in cluster.tables.items()
    }
    result = sql.read("ch", "SELECT front.id FROM db.front", return_metadata=True, retry_cnt=1)
    assert sorted(result.data.id.tolist()) == [20, 30]
    assert result.metadata.data_complete is True
    assert {
        key: (table.uuid, table.ddl, list(table.rows)) for key, table in cluster.tables.items()
    } == original
    assert not cluster.commands


def test_native_read_preserves_explicit_unowned_facade_replica_selection(outage: Any) -> None:
    cluster = _unowned_facade(outage, routed=False)
    result = sql.read("ch", "SELECT id FROM db.front", retry_cnt=1)
    assert result.id.tolist() == [20]
    assert not cluster.commands


def test_automatic_unowned_facade_read_rejects_unverifiable_projection(outage: Any) -> None:
    cluster = _unowned_facade(outage, routed=True, mismatch=True)
    with pytest.raises(sql.ClickHouseClusterTopologyError, match=r"column|schema|projection"):
        sql.read("ch", "SELECT id FROM db.front", retry_cnt=1)
    assert not cluster.selected_reads
    assert not cluster.commands


def test_automatic_managed_facade_read_rejects_unverifiable_projection(outage: Any) -> None:
    cluster = _unowned_facade(outage, routed=True, mismatch=True, physical="front_shard")
    with pytest.raises(sql.ClickHouseClusterTopologyError, match=r"column|schema|projection"):
        sql.read("ch", "SELECT id FROM db.front", retry_cnt=1)
    assert not cluster.selected_reads
    assert not cluster.commands
