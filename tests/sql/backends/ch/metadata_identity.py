"""Distributed storage identity follows the dialect's actual string literals."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.metadata import (
    extract_clickhouse_distributed_shard_table,
    normalize_clickhouse_distributed_database_arg,
    normalize_clickhouse_engine_arg,
)
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one

from tests.sql._support.ch_metadata_identity import NamespaceClient
from tests.sql._support.reconfigure import reconfigure_api


def distributed_engine(database_argument):
    return exp.Anonymous(
        this="Distributed",
        expressions=[
            exp.Literal.string("core"),
            database_argument,
            exp.Literal.string("events_shard"),
            exp.Anonymous(this="rand"),
        ],
    ).sql(dialect=ClickHouseDialect)


@pytest.mark.parametrize("value", ["a'b", "a" + "\\" + "b", "a" + "\\" + "'b", " padded ", "Ω"])
def test_metadata_identity_decodes_the_actual_clickhouse_literal_value(value):
    rendered = exp.Literal.string(value).sql(dialect=ClickHouseDialect)
    literal = parse_one(f"SELECT {rendered}", read=ClickHouseDialect).expressions[0]
    assert isinstance(literal, exp.Literal)
    assert literal.is_string
    assert literal.this == value

    assert normalize_clickhouse_engine_arg(rendered) == value


@pytest.mark.parametrize("database", ["a'b", "a" + "\\" + "b", "a" + "\\" + "'b", " padded ", "Ω"])
def test_metadata_identity_extracts_exact_dialect_rendered_storage_database(database):
    engine = distributed_engine(exp.Literal.string(database))
    parsed = parse_one(f"SELECT {engine}", read=ClickHouseDialect).expressions[0]
    assert isinstance(parsed, exp.Anonymous)
    assert parsed.expressions[1].this == database

    reference = extract_clickhouse_distributed_shard_table(engine, "different")

    assert reference is not None
    assert (reference.cluster, reference.database, reference.table) == (
        "core",
        database,
        "events_shard",
    )


@pytest.mark.parametrize("function", ["currentDatabase", "database"])
def test_metadata_identity_current_database_preserves_the_exact_metadata_identifier(function):
    database = " padded "
    expression = exp.Anonymous(this=function).sql(dialect=ClickHouseDialect)

    assert normalize_clickhouse_distributed_database_arg(expression, database) == database
    reference = extract_clickhouse_distributed_shard_table(
        distributed_engine(exp.Anonymous(this=function)), database
    )
    assert reference is not None
    assert reference.database == database


def test_metadata_identity_quoted_function_spelling_remains_a_literal_database_name():
    database = "currentDatabase()"
    literal = exp.Literal.string(database).sql(dialect=ClickHouseDialect)

    assert normalize_clickhouse_distributed_database_arg(literal, "other") == database
    reference = extract_clickhouse_distributed_shard_table(
        distributed_engine(exp.Literal.string(database)), "other"
    )
    assert reference is not None
    assert reference.database == database


def test_metadata_identity_supports_a_dialect_without_the_optional_database_ast_class(monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.delattr(exp, "CurrentDatabase", raising=False)
        for function in ("currentDatabase()", "database()"):
            assert normalize_clickhouse_distributed_database_arg(function, " padded ") == " padded "
        assert (
            normalize_clickhouse_distributed_database_arg("'currentDatabase()'", "other")
            == "currentDatabase()"
        )


@pytest.mark.parametrize(
    "engine",
    [
        "Distributed('core', , 'db', 'events_shard')",
        "Distributed('core', database(1), 'events_shard')",
        "Distributed('core', database(), 'events_shard') + extra()",
        "Distributed('core', database(), 'events_shard'); SELECT 1",
    ],
)
def test_metadata_identity_rejects_incomplete_or_nonstatic_engine_calls(engine):
    assert extract_clickhouse_distributed_shard_table(engine, "db") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("(abc(", "(abc("), ("'it''s'", "it's"), ('"a""b"', 'a"b'), ("   ", None)],
)
def test_metadata_identity_preserves_exported_raw_argument_compatibility(value, expected):
    assert normalize_clickhouse_engine_arg(value) == expected


def test_metadata_identity_public_plan_rejects_a_dialect_escaped_foreign_database(
    monkeypatch,
):
    facade_database = "a" + "\\" + "'b"
    storage_database = "a'b"
    client = NamespaceClient(facade_database, storage_database)
    engine = parse_one(f"SELECT {client.engineSQL}", read=ClickHouseDialect).expressions[0]
    assert isinstance(engine, exp.Anonymous)
    assert engine.expressions[1].this == storage_database
    table = parse_one(f"SELECT * FROM {client.facade_tableSQL}", read=ClickHouseDialect).find(
        exp.Table
    )
    assert table is not None
    assert table.db == facade_database
    assert facade_database != storage_database
    before = {
        identity: (resource.uuid, resource.ddl, list(resource.rows))
        for identity, resource in client.resources.items()
    }
    monkeypatch.setattr(reconfigure_api, "get_sql_connection", lambda _key: client)

    with pytest.raises(InvalidSqlInputError, match="not a managed Distributed/_shard pair"):
        sql.ch_reconfigure_table(
            "ch",
            client.facade_tableSQL,
            order_by="id + 1",
            ch_distributed_on_cluster="core",
            retry_cnt=1,
            timeout_increment=0,
            dry_run=True,
        )

    assert client.commands == []
    assert {
        identity: (resource.uuid, resource.ddl, list(resource.rows))
        for identity, resource in client.resources.items()
    } == before
    assert client.resources[(storage_database, "events_shard")].rows == [(1,), (2,), (3,)]
    assert client.resources[(facade_database, "events_shard")].rows == [
        (value,) for value in range(1001, 1101)
    ]
    assert client.closed is True
