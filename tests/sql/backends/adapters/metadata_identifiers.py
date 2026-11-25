from __future__ import annotations

import importlib
from types import SimpleNamespace

import pandas as pd
from analytics_toolkit.sql.backends import get_backend
from analytics_toolkit.sql.backends.gp import partition_maintenance
from analytics_toolkit.sql.backends.metadata import (
    build_trino_completion_query,
    build_trino_show_tables_query,
)
from analytics_toolkit.sql.backends.trino import operations as trino_operations


def test_trino_metadata_quotes_each_catalog_and_relation_component():
    adapter = get_backend("trino")
    statements = []
    cursor = SimpleNamespace(
        execute=lambda sql, *_args: statements.append(sql),
        fetchone=lambda: (1,),
        fetchall=lambda: [("id", "bigint")],
        close=lambda: None,
    )
    connection = SimpleNamespace(cursor=lambda: cursor)
    table = '"catalog.dot"."schema.dot"."table.dot"'
    assert adapter.table_exists(connection, table, connection_key="trino")
    assert adapter.get_table_column_types(connection, table, connection_key="trino") == {
        "id": "bigint"
    }
    assert all('FROM "catalog.dot".information_schema.' in statement for statement in statements)
    ddl_calls = []
    trino_operations.extract_table_ddl(
        adapter,
        "trino",
        table,
        read_sql=lambda key, statement: (
            ddl_calls.append(statement) or pd.DataFrame({"ddl": ["CREATE TABLE x"]})
        ),
    )
    assert ddl_calls == ['SHOW CREATE TABLE "catalog.dot"."schema.dot"."table.dot"']


def test_gp_partition_discovery_preserves_raw_dotted_and_quoted_names(monkeypatch):

    io = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")
    monkeypatch.setattr(
        io,
        "read_sql",
        lambda *_args, **_kwargs: pd.DataFrame(
            {
                "schema_name": ["schema.dot"],
                "relation_name": ['table."dot'],
            }
        ),
    )
    options = SimpleNamespace(
        connection_key="gp", retry_cnt=1, timeout_increment=0, query_label=None
    )
    assert partition_maintenance._discover(options, "public.parent") == [
        '"schema.dot"."table.""dot"'
    ]


def test_trino_metadata_builder_retains_quoted_compatibility_and_raw_catalog_identity():

    ordinary = build_trino_show_tables_query("catalog.dot", None, None, None)
    assert 'FROM "catalog.dot".information_schema.tables' in ordinary
    quoted = build_trino_completion_query('"catalog.dot"', None, "")
    assert 'FROM "catalog.dot".information_schema.tables' in quoted
    raw_quoted_parts = build_trino_completion_query('"catalog"."dot"', None, "")
    assert 'FROM """catalog"".""dot""".information_schema.tables' in raw_quoted_parts
