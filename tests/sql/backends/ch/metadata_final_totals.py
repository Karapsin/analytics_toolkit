"""Public metadata predicates apply after complete logical statistics resolve."""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from analytics_toolkit import sql

from tests.sql._support.ch_metadata_sqlite import MetadataServer

show_tables_module = importlib.import_module("analytics_toolkit.sql.metadata.show_tables")


@pytest.mark.parametrize(
    ("condition", "expected"),
    [("row_count > 100", [120]), ("row_count < 100", []), ("table_size_bytes > 1000", [120])],
)
def test_public_metadata_filters_final_logical_totals(
    outage: Any, monkeypatch: Any, condition: str, expected: list[int]
) -> None:
    server = MetadataServer(
        [(1, 1), (2, 1)],
        [
            (1, "db", "events", 60, 600, "MergeTree", "MergeTree"),
            (2, "db", "events", 60, 600, "MergeTree", "MergeTree"),
        ],
    )
    monkeypatch.setattr(show_tables_module, "read_sql", server.read)
    try:
        result = sql.show_tables("ch", conditions=condition)
        assert not server.errors
        assert result.row_count.tolist() == expected
    finally:
        server.close()


def test_public_metadata_preserves_unknown_view_estimates(outage: Any, monkeypatch: Any) -> None:
    server = MetadataServer(
        [(1, 1), (2, 1)],
        [
            (1, "db", "visible", None, None, "View", "View"),
            (2, "db", "visible", None, None, "View", "View"),
        ],
    )
    monkeypatch.setattr(show_tables_module, "read_sql", server.read)
    try:
        result = sql.show_tables("ch")
        assert not server.errors
        assert result.row_count.tolist() == [None]
        assert result.table_size.tolist() == [None]
    finally:
        server.close()


def test_public_metadata_flags_missing_relation_on_reachable_shard(
    outage: Any, monkeypatch: Any
) -> None:
    server = MetadataServer(
        [(1, 1), (2, 1)],
        [(1, "db", "events", 60, 600, "MergeTree", "MergeTree")],
    )
    monkeypatch.setattr(show_tables_module, "read_sql", server.read)
    try:
        result = sql.show_tables("ch")
        assert not server.errors
        assert result.row_count.tolist() == [60]
        assert result.attrs.get("data_complete") is False
    finally:
        server.close()


def test_public_metadata_uses_healthy_replicated_representative(
    outage: Any, monkeypatch: Any
) -> None:
    server = MetadataServer(
        [(1, 1), (1, 2)],
        [
            (1, "db", "events", 1, 10, "ReplicatedMergeTree", "ReplicatedMergeTree"),
            (2, "db", "events", 42, 420, "ReplicatedMergeTree", "ReplicatedMergeTree"),
        ],
    )
    server.connection.execute(
        "UPDATE host_replicas SET is_session_expired = 1 WHERE _shard_num = 1"
    )
    monkeypatch.setattr(show_tables_module, "read_sql", server.read)
    try:
        result = sql.show_tables("ch")
        assert not server.errors
        assert result.row_count.tolist() == [42]
    finally:
        server.close()
