from __future__ import annotations

import importlib

import pytest

gp_adapter = importlib.import_module("analytics_toolkit.sql.backends.gp.adapter")
trino_adapter = importlib.import_module(
    "analytics_toolkit.sql.backends.trino.adapter"
)
table_ops = importlib.import_module("analytics_toolkit.sql.dml.table._basic_ops")
ddl_identifiers = importlib.import_module("analytics_toolkit.sql.ddl.identifiers")


def test_gp_identifier_parser_preserves_quoted_dots_and_public_default() -> None:
    assert gp_adapter.split_gp_table_name("events") == ("public", "events")
    assert gp_adapter.split_gp_table_name(
        '"schema.with.dot"."table.with.dot"'
    ) == ("schema.with.dot", "table.with.dot")


def test_trino_identifier_parser_preserves_quoted_dots_and_defaults() -> None:
    assert trino_adapter.split_trino_table_name(
        '"catalog.with.dot"."schema.with.dot"."table.with.dot"'
    ) == ("catalog.with.dot", "schema.with.dot", "table.with.dot")
    assert trino_adapter.split_trino_table_name("mart.events") == (
        "iceberg",
        "mart",
        "events",
    )
    assert trino_adapter.split_trino_table_name("events") == (
        "iceberg",
        "sandbox",
        "events",
    )


@pytest.mark.parametrize("connection_type", ["gp", "trino", "ch"])
def test_qualified_identifier_quoting_preserves_dots_inside_parts(
    connection_type: str,
) -> None:
    quoted = table_ops.quote_qualified_table_name(
        '"schema.with.dot"."table.with.dot"',
        connection_type,
    )
    quote = "`" if connection_type == "ch" else '"'
    assert quoted == (
        f"{quote}schema.with.dot{quote}.{quote}table.with.dot{quote}"
    )


def test_identifier_parsers_reject_invalid_arity() -> None:
    with pytest.raises(ValueError, match="Invalid Greenplum table name"):
        gp_adapter.split_gp_table_name("catalog.schema.table")
    with pytest.raises(ValueError, match="Invalid table name"):
        trino_adapter.split_trino_table_name("server.catalog.schema.table")


def test_ddl_identifier_helpers_reject_invalid_parse_results(monkeypatch) -> None:
    monkeypatch.setattr(
        ddl_identifiers,
        "parse_one",
        lambda *args, **kwargs: ddl_identifiers.exp.Literal.number(1),
    )
    with pytest.raises(ValueError, match="Invalid table name"):
        ddl_identifiers._parse_table_name("schema.table", "postgres")
    with pytest.raises(ValueError, match="Invalid table identifier"):
        ddl_identifiers._identifier_name(ddl_identifiers.exp.Literal.number(1))
