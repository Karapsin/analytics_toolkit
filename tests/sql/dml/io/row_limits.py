from __future__ import annotations

import importlib
from typing import Any

import pandas as pd
import pytest
import sqlglot
from analytics_toolkit import sql
from analytics_toolkit._sql_statements import split_statements
from analytics_toolkit.sql.backends import row_limits
from analytics_toolkit.sql.backends.row_limits import apply_row_limit
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp
from sqlparse import tokens
from sqlparse.sql import Token

from tests.sql._support.fakes import FakeDbapiConnection
from tests.sql.dml.io.execute.read import FakeClickHouseClient

read_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")
execute_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_read")
DIALECTS = [("gp", "postgres"), ("trino", "trino"), ("ch", "clickhouse")]


@pytest.mark.parametrize("dialect", [dialect for _, dialect in DIALECTS])
@pytest.mark.parametrize(
    "query",
    [
        "select value from orders order by value",
        "select value from orders limit 500 offset 3",
        "select * from (select value from orders limit 7) q",
        "with q as (select 1 as value limit 7) select * from q",
        "-- header\nselect 'limit 500; -- literal' as value; -- tail",
        "select 1 /* internal */; /* tail */",
    ],
)
def test_limit_changes_only_outer_result(query: str, dialect: str) -> None:
    changed, capped = apply_row_limit(query, 201, dialect=dialect)
    assert capped
    original = sqlglot.parse_one(split_statements(query)[0], read=dialect)
    result = sqlglot.parse_one(split_statements(changed)[0], read=dialect)
    assert result.args["limit"].expression.this == "201"
    original.set("limit", None)
    result.set("limit", None)
    assert original == result
    for comment in ("header", "tail", "internal"):
        if comment in query:
            assert comment in changed
    assert "analytics_toolkit_explorer_result" not in changed


@pytest.mark.parametrize("limit", [0, 10, 201])
def test_existing_smaller_limit_is_unchanged(limit: int) -> None:
    query = f"select 1 limit {limit}; -- tail"
    assert apply_row_limit(query, 201, dialect="postgres") == (query, True)


@pytest.mark.parametrize(
    ("dialect", "query"),
    [
        ("postgres", "show work_mem"),
        ("postgres", "explain select * from orders"),
        ("postgres", "update orders set reviewed = true returning id"),
        ("postgres", "with q as (select 1) delete from orders returning id"),
        ("postgres", "select * into copied from orders"),
        ("postgres", "select * from orders limit (select 2)"),
        ("postgres", "select * from orders fetch first 10 rows with ties"),
        ("postgres", "select 1\nas select 1"),
        ("postgres", "/* empty */"),
        ("clickhouse", "select * from orders limit 2 by id"),
        ("clickhouse", "select 1 format JSON"),
    ],
)
def test_unsupported_queries_pass_through(dialect: str, query: str) -> None:
    assert apply_row_limit(query, 201, dialect=dialect) == (query, False)


def test_fetch_and_backend_clauses_are_preserved() -> None:
    query, capped = apply_row_limit(
        "select * from orders offset 2 rows fetch first 500 rows only", 201, dialect="postgres"
    )
    assert capped
    tree = sqlglot.parse_one(query, read="postgres")
    assert tree.args["limit"].args["count"].this == "201"
    assert tree.args["offset"].expression.this == "2"
    query, capped = apply_row_limit(
        "select * from orders settings max_threads=1", 201, dialect="clickhouse"
    )
    assert capped
    assert "LIMIT 201\nsettings max_threads=1" in query
    query, capped = apply_row_limit("select * from orders for update", 201, dialect="postgres")
    assert capped
    assert query.endswith("LIMIT 201\nfor update")


@pytest.mark.parametrize("entrypoint", ["read", "execute_read"])
@pytest.mark.parametrize("limit", [True, False, 0, -1, 1.5, "2"])
def test_invalid_limit_never_opens_connection(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str, limit: Any
) -> None:
    module = read_module if entrypoint == "read" else execute_module
    monkeypatch.setattr(module, "get_sql_connection", lambda _: pytest.fail("opened connection"))
    with pytest.raises(InvalidSqlInputError, match="row_limit"):
        getattr(sql, entrypoint)("gp", "select 1", row_limit=limit)


@pytest.mark.parametrize(("backend", "dialect"), DIALECTS)
@pytest.mark.parametrize("output_type", ["df", "scalar", "list", "dict"])
def test_read_limit_reaches_driver_and_preserves_outputs(
    monkeypatch: pytest.MonkeyPatch, backend: str, dialect: str, output_type: str
) -> None:
    connection = (
        FakeClickHouseClient(pd.DataFrame({"value": [7]}))
        if backend == "ch"
        else FakeDbapiConnection(rows=[(7,)], description=[("value",)])
    )
    monkeypatch.setattr(read_module, "get_sql_connection", lambda _: connection)
    result = sql.read(
        backend,
        "select value from orders",
        row_limit=1,
        output_type=output_type,
        return_metadata=True,
        query_label="preview",
        retry_cnt=1,
    )
    queries = connection.read_queries if backend == "ch" else connection.executed
    assert sqlglot.parse_one(queries[0], read=dialect).args["limit"].expression.this == "1"
    assert "preview" in queries[0]
    assert result.rows == result.metadata.read_rows == result.metadata.source_rows == 1
    if output_type == "df":
        assert result.data.to_dict("list") == {"value": [7]}
    else:
        assert result.data == {"scalar": 7, "list": [7], "dict": {"value": [7]}}[output_type]
    assert connection.close_calls == 1


@pytest.mark.parametrize(("backend", "dialect"), DIALECTS)
@pytest.mark.parametrize("row_limit", [None, 201])
def test_execute_read_limit_applies_only_to_final_statement_of_each_batch_item(
    monkeypatch: pytest.MonkeyPatch, backend: str, dialect: str, row_limit: int | None
) -> None:
    connections: list[Any] = []

    def connect(_: str) -> Any:
        connection = (
            FakeClickHouseClient()
            if backend == "ch"
            else FakeDbapiConnection(rows=[(1,)], description=[("value",)])
        )
        connections.append(connection)
        return connection

    monkeypatch.setattr(execute_module, "get_sql_connection", connect)
    result = sql.execute_read(
        backend,
        [
            "create temporary table tmp as select 1; select * from tmp",
            "select 1",
        ],
        row_limit=row_limit,
        retry_cnt=1,
        return_metadata=True,
    )
    assert len(result) == len(connections) == 2
    for connection in connections:
        queries = (
            connection.commands + connection.read_queries
            if backend == "ch"
            else connection.executed
        )
        for statement in sqlglot.parse(";".join(queries), read=dialect):
            if isinstance(statement, exp.Create):
                assert statement.expression.args.get("limit") is None
            else:
                assert bool(statement.args.get("limit")) is (row_limit is not None)
    assert all(item.rows == 1 for item in result)


def test_metadata_helper_and_csv_use_the_same_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    connection = FakeDbapiConnection(rows=[(7,)], description=[("value",)])
    monkeypatch.setattr(read_module, "get_sql_connection", lambda _: connection)
    path = tmp_path / "limited.csv"
    result = read_module.read_sql_with_metadata(
        "gp", "select value from orders", row_limit=1, to_csv=str(path), retry_cnt=1
    )
    assert result.rows == 1
    assert "LIMIT 1" in connection.executed[0]
    assert path.read_text().splitlines() == ["value", "7"]


def test_default_read_preserves_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = FakeDbapiConnection(rows=[(1,)], description=[("value",)])
    monkeypatch.setattr(read_module, "get_sql_connection", lambda _: connection)
    sql.read("gp", "select 1", retry_cnt=1)
    assert connection.executed == ["select 1"]


@pytest.mark.parametrize(
    ("dialect", "capped"), [("postgres", True), ("trino", True), ("clickhouse", False)]
)
def test_union_never_introduces_a_wrapper(dialect: str, capped: bool) -> None:
    query = "select 1 union all select 2"
    changed, actual = apply_row_limit(query, 201, dialect=dialect)
    assert actual is capped
    assert changed.startswith(query)
    assert "FROM (" not in changed
    if capped:
        assert sqlglot.parse_one(changed, read=dialect).args["limit"].expression.this == "201"
    else:
        assert changed == query


def test_parser_disagreement_leaves_sql_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    tokenize = row_limits.sql_tokens

    def unrecognized_count(source: str) -> list[Token]:
        return [
            Token(tokens.Name, token.value) if token.value == "500" else token
            for token in tokenize(source)
        ]

    # A dialect extension or tokenizer version may recognize SQL but not its count token.
    monkeypatch.setattr(row_limits, "sql_tokens", unrecognized_count)
    query = "select 1 limit 500"
    assert apply_row_limit(query, 201, dialect="postgres") == (query, False)


def test_values_remain_usable_with_older_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    parse = sqlglot.parse_one

    def parse_without_values_limit(source: str, **kwargs: Any) -> Any:
        if source.startswith("values") and "LIMIT" in source:
            message = "VALUES LIMIT is unsupported by this parser."
            raise sqlglot.errors.ParseError(message)
        return parse(source, **kwargs)

    monkeypatch.setattr(row_limits.sqlglot, "parse_one", parse_without_values_limit)
    query = "values (1), (2)"
    assert apply_row_limit(query, 201, dialect="postgres") == (query, False)
