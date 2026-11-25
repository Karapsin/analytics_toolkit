"""Decode known control metadata without changing native user String payloads."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.managed_routing import _query_rows
from analytics_toolkit.sql.backends.ch.metadata_values import metadata_text
from analytics_toolkit.sql.backends.ch.outage_topology import query_rows
from sqlglot import exp, parse_one

from tests.sql._support.ch_statistics_boundary import install_statistics_boundary
from tests.sql._support.outage import Client, Result


def test_known_metadata_text_decodes_exact_names_and_rejects_unverifiable_bytes() -> None:
    assert metadata_text(" Events ") == " Events "
    assert metadata_text("события".encode()) == "события"
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="UTF-8"):
        metadata_text(b"\xff\x00")


def test_metadata_row_helpers_remain_raw_without_requested_text_fields() -> None:
    row = [b"\xff\x00"]
    client = SimpleNamespace(query=lambda _sql: SimpleNamespace(result_rows=[row]))
    for reader in (query_rows, _query_rows):
        result = reader(client, "SELECT payload FROM sandbox.events")
        assert result == [[b"\xff\x00"]]
        assert result[0] is row


@pytest.mark.parametrize("replicated", [False, True])
def test_native_binary_string_reads_keep_metadata_identity_and_user_bytes_separate(
    outage: Any, monkeypatch: Any, replicated: bool
) -> None:
    cluster, configure = outage
    configure(driver="native", settings={"strings_as_bytes": True})
    cluster.members = [(1, 1, "h1", 9000), (1, 2, "h2", 9000)]
    cluster.tables.clear()
    engine = "ReplicatedMergeTree" if replicated else "MergeTree"
    identity = "12345678-1234-4234-9234-123456789abc"
    for host in ("h1", "h2"):
        cluster.create(
            host,
            f"CREATE TABLE db.records UUID '{identity}' "
            f"(payload String) ENGINE={engine} ORDER BY tuple()",
        )
        cluster.tables[(host, "db.records")].rows = [(b"\xff\x00",)]
    server = install_statistics_boundary(monkeypatch, cluster)
    original = Client.query

    def query(client: Any, statement: str, **kwargs: Any) -> Any:
        tree = parse_one(statement, read=ClickHouseDialect)
        metadata = False
        for source in tree.find_all(exp.Table):
            database = source.db
            if isinstance(source.this, exp.Anonymous) and source.this.name.lower() in {
                "cluster",
                "clusterallreplicas",
            }:
                _cluster, database, _table, _extra = cluster_arguments(source.this, "db")
            metadata = metadata or database == "system"
        result = original(client, statement, **kwargs)
        if not metadata:
            return result
        return Result(
            [
                tuple(value.encode("utf-8") if isinstance(value, str) else value for value in row)
                for row in result.result_rows
            ],
            result.column_names,
            result.column_types,
        )

    monkeypatch.setattr(Client, "query", query)
    try:
        result = sql.read("ch", "SELECT payload FROM db.records", retry_cnt=1)
        assert result.payload.tolist() == ([b"\xff\x00"] if replicated else [b"\xff\x00"] * 2), {
            "submitted": cluster.queries[-1],
            "selected": cluster.selected_reads,
        }
        metadata = sql.show_tables("ch", schema="db", table_name="records")
        assert metadata.table_name.tolist() == ["records"]
        assert metadata.row_count.tolist() == [1 if replicated else 2]
        assert metadata.attrs.get("data_complete", True) is True
        assert not server.errors
    finally:
        server.close()
