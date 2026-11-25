"""Public metadata filters select exact returned namespace and relation names."""

from __future__ import annotations

import importlib
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

show_tables_module = importlib.import_module("analytics_toolkit.sql.metadata.show_tables")


def _install_catalog(
    monkeypatch: pytest.MonkeyPatch, backend: str, schema: str, names: list[str]
) -> list[str]:
    statements: list[str] = []
    dialect: Any = (
        ClickHouseDialect if backend == "ch" else "postgres" if backend == "gp" else "trino"
    )

    def read(_key: str, statement: str) -> pd.DataFrame:
        statements.append(statement)
        tree = parse_one(statement, read=dialect)
        records = [
            {
                "db": "fixture",
                "schema": schema,
                "table_name": name,
                "row_count": 7,
                "table_size_bytes": 70,
            }
            for name in names
        ]
        for condition in tree.find_all(exp.EQ):
            if not isinstance(condition.this, exp.Column) or not isinstance(
                condition.expression, exp.Literal
            ):
                continue
            column = condition.this.name
            value = condition.expression.this
            if column in {"schema", "database", "table_schema"}:
                records = [row for row in records if row["schema"] == value]
            elif column in {"name", "table_name"}:
                records = [row for row in records if row["table_name"] == value]
        for condition in tree.find_all(exp.In):
            if isinstance(condition.this, exp.Column) and condition.this.name in {
                "name",
                "table_name",
            }:
                values = [value.this for value in condition.expressions]
                records = [row for row in records if row["table_name"] in values]
        return pd.DataFrame(records)

    monkeypatch.setattr(show_tables_module, "read_sql", read)
    return statements


@pytest.mark.parametrize(
    ("schema", "name"),
    [
        (r"sales\north", r"Events\next"),
        ("sales\\", "Events\\"),
        (r"sales\'north", r"Events\'next"),
    ],
)
@pytest.mark.parametrize("multiple", [False, True])
def test_clickhouse_show_tables_filters_preserve_backslash_and_quote_identity(
    monkeypatch: pytest.MonkeyPatch, schema: str, name: str, multiple: bool
) -> None:
    names = [name, name + "_second"] if multiple else [name]
    statements = _install_catalog(monkeypatch, "ch", schema, names)

    result = sql.show_tables("ch", schema=schema, table_name=names if multiple else name)

    assert result.schema.tolist() == [schema] * len(names)
    assert result.table_name.tolist() == names
    assert result.row_count.tolist() == [7] * len(names)
    assert len(statements) == 1


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize("multiple", [False, True])
def test_show_tables_preserves_padded_raw_metadata_names(
    monkeypatch: pytest.MonkeyPatch, backend: str, multiple: bool
) -> None:
    schema = " Sales "
    names = [" Events ", " events "] if multiple else [" Events "]
    statements = _install_catalog(monkeypatch, backend, schema, names)

    result = sql.show_tables(backend, schema=schema, table_name=names if multiple else names[0])

    assert result.schema.tolist() == [schema] * len(names)
    assert result.table_name.tolist() == names
    assert len(statements) == 1


def test_show_tables_preserves_padded_raw_trino_catalog_name(monkeypatch):
    catalog = " Catalog "
    statements = []

    def read(_key, statement):
        statements.append(statement)
        tree = parse_one(statement, read="trino")
        metadata = [table for table in tree.find_all(exp.Table) if table.db == "information_schema"]
        assert [table.catalog for table in metadata] == [catalog]
        return pd.DataFrame(
            {
                "db": [catalog],
                "schema": [" Sales "],
                "table_name": [" Events "],
                "row_count": [None],
                "table_size_bytes": [None],
            }
        )

    monkeypatch.setattr(show_tables_module, "read_sql", read)
    result = sql.show_tables(
        "trino", schema=" Sales ", table_name=" Events ", trino_catalog=catalog
    )
    assert result.db.tolist() == [catalog]
    assert result.schema.tolist() == [" Sales "]
    assert result.table_name.tolist() == [" Events "]
    assert len(statements) == 1
