"""An empty table listing retains the coverage evidence of its metadata query."""

from __future__ import annotations

import importlib
from contextlib import nullcontext
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.outage import Client, Result

_EXPECTED_COLUMNS = ["db", "schema", "table_name", "row_count", "table_size"]
_LISTING_COLUMNS = (
    "db",
    "schema",
    "table_name",
    "row_count",
    "table_size_bytes",
    "engine",
    "engine_full",
)


@pytest.fixture
def table_listing_transport(outage: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    cluster, _configure = outage
    original_query = Client.query

    def query(client: Client, statement: str, **kwargs: Any) -> Result:
        tree = parse_one(statement, read=ClickHouseDialect)
        listing = tree.find(exp.Subquery)
        if listing is None or listing.alias != "table_metadata":
            return original_query(client, statement, **kwargs)
        client.cluster.queries.append(statement)
        filters = {
            equality.left.name: str(equality.right.this)
            for equality in tree.find_all(exp.EQ)
            if isinstance(equality.right, exp.Literal)
        }
        rows = []
        for (host, qualified_name), table in client.cluster.tables.items():
            database, name = qualified_name.rsplit(".", 1)
            if host not in client.cluster.alive:
                continue
            if (
                filters.get("schema", database) != database
                or filters.get("table_name", name) != name
            ):
                continue
            rows.append((database, database, name, 0, 0, table.engine, table.engine_full))
        if not rows and getattr(client.cluster, "listing_columnless_empty", False):
            return Result([], (), ())
        return Result(rows, _LISTING_COLUMNS, ("String",) * len(_LISTING_COLUMNS))

    monkeypatch.setattr(Client, "query", query)
    cluster.create(
        "h2",
        "CREATE TABLE db.remote_only (id UInt64) "
        "ENGINE=Distributed('core','db','events_shard',rand())",
    )
    return cluster


@pytest.mark.parametrize(
    ("missing_host", "table_name", "empty", "columnless"),
    [
        (True, "remote_only", True, False),
        (True, "remote_only", True, True),
        (False, "absent", True, False),
        (False, "absent", True, True),
        (True, "events", False, False),
        (False, "remote_only", False, False),
    ],
)
def test_table_listing_keeps_coverage_for_empty_and_nonempty_results(
    table_listing_transport: Any,
    missing_host: bool,
    table_name: str,
    empty: bool,
    columnless: bool,
) -> None:
    cluster = table_listing_transport
    cluster.listing_columnless_empty = columnless
    if missing_host:
        cluster.alive.remove("h2")
    warning = pytest.warns(sql.ClickHouseDegradedClusterWarning) if missing_host else nullcontext()
    with warning:
        tables = sql.show_tables(
            "ch",
            schema="db",
            table_name=table_name,
            ch_distributed_table_stats=columnless,
            ch_unavailable_shards="healthy_shards",
        )
    assert isinstance(tables, pd.DataFrame)
    assert tables.empty is empty
    assert list(tables.columns) == _EXPECTED_COLUMNS
    assert tables.attrs["data_complete"] is (not missing_host)
    statuses = tables.attrs["ch_cluster_status"]
    assert statuses
    assert all(status["coverage_intent"] == "physical_hosts" for status in statuses)
    assert any(bool(status["excluded_hosts"]) is missing_host for status in statuses)
    assert sum("table_metadata" in query for query in cluster.queries) == 1
    assert all("host_scope" not in query and "getMacro" not in query for query in cluster.queries)
    if empty:
        assert list(tables.index) == []
    else:
        assert tables.table_name.tolist() == [table_name]


@pytest.mark.parametrize("backend", ["gp", "trino"])
@pytest.mark.parametrize("empty", [False, True])
def test_other_backend_table_listing_shapes_and_metadata_defaults_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    empty: bool,
) -> None:
    returned = (
        pd.DataFrame()
        if empty
        else pd.DataFrame(
            {
                "db": ["warehouse"],
                "schema": ["public"],
                "table_name": ["events"],
                "row_count": [7],
                "table_size_bytes": [128],
            }
        )
    )
    module = importlib.import_module("analytics_toolkit.sql.metadata.show_tables")
    monkeypatch.setattr(module, "read_sql", lambda *_args, **_kwargs: returned.copy())
    tables = sql.show_tables(backend)
    assert isinstance(tables, pd.DataFrame)
    assert tables.empty is empty
    assert list(tables.columns) == _EXPECTED_COLUMNS
    assert tables.attrs == {}
    if not empty:
        assert tables.row_count.tolist() == [7]
        assert tables.table_size.tolist() == ["128 B"]
