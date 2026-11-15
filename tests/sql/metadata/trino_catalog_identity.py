"""Public metadata catalogs are raw names, including literal quote characters."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
import sqlglot
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends.metadata import (
    build_trino_completion_query,
    build_trino_show_tables_query,
)
from sqlglot import exp


def _metadata_catalog(query):
    parsed = sqlglot.parse_one(query, read="trino")
    tables = [table for table in parsed.find_all(exp.Table) if table.db == "information_schema"]
    assert len(tables) == 1
    assert tables[0].name == "tables"
    return tables[0].catalog


def _install_metadata_client(monkeypatch):
    statements = []
    opened = []
    closed = []
    selected = []

    def execute(query):
        statements.append(query)
        selected.append(_metadata_catalog(query))

    cursor = SimpleNamespace(
        description=[
            ("db",),
            ("schema",),
            ("table_name",),
            ("row_count",),
            ("table_size_bytes",),
        ],
        execute=execute,
        fetchall=lambda: [(selected[-1], "mart", "orders", 17, None)],
        close=lambda: closed.append("cursor"),
    )
    connection = SimpleNamespace(
        cursor=lambda: cursor,
        close=lambda: closed.append("connection"),
    )

    def connect(alias):
        opened.append(alias)
        return connection

    read_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")
    monkeypatch.setattr(read_module, "get_sql_connection", connect)
    return statements, opened, closed


@pytest.mark.parametrize(
    "catalog",
    ['"sales"', "sales", "sales.region", "select", 'sa"les'],
    ids=["literal-quotes", "ordinary", "literal-dot", "reserved", "embedded-quote"],
)
@pytest.mark.parametrize("catalog_source", ["argument", "configuration"])
def test_show_tables_preserves_raw_trino_catalog_identity(
    monkeypatch, write_sql_connections, catalog, catalog_source
):
    set_connections_path(
        write_sql_connections(
            {
                "warehouse": {
                    "type": "trino",
                    "host": "trino.example",
                    "port": 8080,
                    "user": "reader",
                    "catalog": catalog if catalog_source == "configuration" else "fallback",
                    "schema": "mart",
                }
            }
        )
    )
    statements, opened, closed = _install_metadata_client(monkeypatch)
    kwargs = {"trino_catalog": catalog} if catalog_source == "argument" else {}

    result = sql.show_tables("warehouse", schema="mart", **kwargs)

    assert len(statements) == 1
    assert _metadata_catalog(statements[0]) == catalog
    assert result["db"].tolist() == [catalog]
    assert result["table_name"].tolist() == ["orders"]
    assert result["row_count"].tolist() == [17]
    assert opened == ["warehouse"]
    assert closed == ["cursor", "connection"]


def test_legacy_metadata_builders_still_accept_a_rendered_catalog_identifier():
    assert _metadata_catalog(build_trino_show_tables_query('"sales"', None, None, None)) == "sales"
    assert _metadata_catalog(build_trino_completion_query('"sales"', None, "")) == "sales"
