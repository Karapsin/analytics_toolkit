"""Logical metadata totals and exact table identities survive routed collection."""

from __future__ import annotations

import importlib
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.metadata import (
    ClickHouseShardTable,
    build_clickhouse_shard_stats_query,
)
from analytics_toolkit.sql.backends.ch.outage_context import logical_statistics
from analytics_toolkit.sql.backends.ch.routing import ChClusterRouting, route_sql
from sqlglot import exp, parse_one

show_tables_module = importlib.import_module("analytics_toolkit.sql.metadata.show_tables")


@pytest.mark.parametrize("expected_count", [42, 2**53 + 1])
def test_routed_show_tables_deduplicates_exact_identity_and_keeps_case(
    outage: Any, monkeypatch: Any, expected_count: int
) -> None:
    _cluster, _configure = outage

    def read(_key: str, query: str) -> pd.DataFrame:
        if "host_scope" in query:
            return pd.DataFrame(
                {
                    "shard_database": ["db", "db"],
                    "shard_table": ["Events", "events"],
                    "row_count": [expected_count, 7],
                    "table_size_bytes": [420, 70],
                }
            )
        return pd.DataFrame(
            {
                "db": ["db"] * 4,
                "schema": ["db"] * 4,
                "table_name": ["Events", "events", "Events", "events"],
                "engine": ["ReplicatedMergeTree"] * 4,
                "engine_full": [""] * 4,
                "row_count": [21, 3, 21, 4],
                "table_size_bytes": [210, 30, 210, 40],
            }
        )

    monkeypatch.setattr(show_tables_module, "read_sql", read)
    result = sql.show_tables("ch")
    assert result.table_name.tolist() == ["Events", "events"]
    assert result.row_count.tolist() == [expected_count, 7]


def test_logical_statistics_counts_replicated_shards_and_independent_hosts() -> None:
    query = build_clickhouse_shard_stats_query(
        "core", {ClickHouseShardTable("core", "db", "events")}
    )
    tree = parse_one(query, read="clickhouse")
    assert len(list(tree.find_all(exp.ArgMin))) == 2
    assert "host_scope.shard_num" in query
    assert "startsWith(engine, 'Replicated')" in query
    assert "startsWith(engine, 'Shared')" in query
    assert "sum(ifNull(total_rows, 0))" in query
    assert "ORDER BY shard_num, replica_num" in query
    with logical_statistics():
        assert route_sql(query, routing=ChClusterRouting("other"), database="default") == query
    # The statistics intent does not affect an ordinary user wrapper.
    assert "clusterAllReplicas" in route_sql(
        "SELECT * FROM clusterAllReplicas('core', 'db', 'events')",
        routing=ChClusterRouting("other"),
        database="default",
    )


def test_failed_statistics_never_present_facade_defaults_as_complete(monkeypatch: Any) -> None:
    def read(_key: str, query: str) -> pd.DataFrame:
        if "host_scope" in query:
            message = "offline"
            raise ConnectionError(message)
        return pd.DataFrame(
            {
                "db": ["db"],
                "schema": ["db"],
                "table_name": ["events"],
                "engine": ["Distributed"],
                "engine_full": ["Distributed('core','db','events_shard',rand())"],
                "row_count": [None],
                "table_size_bytes": [0],
            }
        )

    monkeypatch.setattr(show_tables_module, "read_sql", read)
    result = sql.show_tables("ch", ch_distributed_table_stats=True)
    assert result.attrs["data_complete"] is False
    assert result.row_count.tolist() == [None]


def test_facade_only_listing_does_not_collect_unrequested_storage_totals(
    outage: Any, monkeypatch: Any
) -> None:
    calls = []

    def read(_key: str, query: str) -> pd.DataFrame:
        calls.append(query)
        return pd.DataFrame(
            {
                "db": ["db"],
                "schema": ["db"],
                "table_name": ["front"],
                "engine": ["Distributed"],
                "engine_full": ["Distributed('core','db','actual',rand())"],
                "row_count": [None],
                "table_size_bytes": [0],
            }
        )

    monkeypatch.setattr(show_tables_module, "read_sql", read)
    result = sql.show_tables("ch", ch_distributed_table_stats=False)
    assert result.table_name.tolist() == ["front"]
    assert len(calls) == 1


def test_partial_table_statistics_keep_missing_estimates_and_incomplete_notice(
    outage: Any, monkeypatch: Any
) -> None:
    def read(_key: str, query: str) -> pd.DataFrame:
        if "host_scope" in query:
            return pd.DataFrame(
                {
                    "shard_database": ["db"],
                    "shard_table": ["first"],
                    "row_count": [42],
                    "table_size_bytes": [420],
                }
            )
        return pd.DataFrame(
            {
                "db": ["db", "db"],
                "schema": ["db", "db"],
                "table_name": ["first", "second"],
                "engine": ["MergeTree", "MergeTree"],
                "engine_full": ["", ""],
                "row_count": [1, 7],
                "table_size_bytes": [10, 70],
            }
        )

    monkeypatch.setattr(show_tables_module, "read_sql", read)
    result = sql.show_tables("ch")
    assert result.row_count.tolist() == [42, 7]
    assert result.attrs["data_complete"] is False
