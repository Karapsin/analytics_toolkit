"""Routed sources retain qualified identity when relation names repeat in one scope."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.managed_routing import ManagedTableRoute
from analytics_toolkit.sql.backends.ch.routing import ChClusterRouting, wrap_client
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp, parse_one


@pytest.fixture
def routed_queries(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: list[str] = []

    def query(statement: str, **_kwargs: Any) -> Any:
        captured.append(statement)
        return SimpleNamespace(
            column_names=("left_id", "right_id"),
            result_columns=([41], [42]),
            result_rows=[(41, 42)],
            row_count=1,
        )

    raw = SimpleNamespace(query=query, close=lambda: None)
    connection = wrap_client(
        raw, SimpleNamespace(cluster_routing=ChClusterRouting("core"), database="default")
    )
    resolver = SimpleNamespace(
        resolve=lambda *, cluster, database, table: ManagedTableRoute(database, table, "cluster")
    )
    object.__setattr__(connection, "_managed_pairs", resolver)
    for module in ("read_sql", "execute_read"):
        monkeypatch.setattr(
            importlib.import_module(f"analytics_toolkit.sql.dml.io.{module}"),
            "get_sql_connection",
            lambda _key: connection,
        )
    return captured


@pytest.mark.parametrize("entrypoint", ["read", "execute_read"])
@pytest.mark.parametrize(
    ("left_database", "right_database"), [("a", "b"), ("a.with.dot", "b.with.dot")]
)
@pytest.mark.parametrize("stars", [False, True])
def test_same_scope_database_qualified_sources_remain_distinct(
    routed_queries: list[str],
    entrypoint: str,
    left_database: str,
    right_database: str,
    stars: bool,
) -> None:
    left = f"`{left_database}`.events"
    right = f"`{right_database}`.events"
    projections = f"{left}.*, {right}.*" if stars else f"{left}.id, {right}.id"
    query = f"SELECT {projections} FROM {left} JOIN {right} ON {left}.id={right}.id"
    result = getattr(sql, entrypoint)("ch", query, retry_cnt=1)
    assert result.to_dict("list") == {"left_id": [41], "right_id": [42]}
    assert len(routed_queries) == 1
    tree = parse_one(routed_queries[0], read=ClickHouseDialect)
    sources = {
        table.this.expressions[1].this: table.alias
        for table in tree.find_all(exp.Table)
        if isinstance(table.this, exp.Anonymous)
    }
    assert set(sources) == {left_database, right_database}
    assert sources[left_database] != sources[right_database]
    assert tree.expressions[0].table == sources[left_database]
    assert tree.expressions[1].table == sources[right_database]
    join = tree.args["joins"][0].args["on"]
    assert join.left.table == sources[left_database]
    assert join.right.table == sources[right_database]
    assert all(not column.db and not column.catalog for column in tree.find_all(exp.Column))


@pytest.mark.parametrize("entrypoint", ["read", "execute_read"])
def test_same_scope_explicit_aliases_remain_authoritative(
    routed_queries: list[str],
    entrypoint: str,
) -> None:
    getattr(sql, entrypoint)(
        "ch",
        "SELECT l.id, r.id, l.*, r.* FROM a.events AS l JOIN b.events AS r ON l.id=r.id",
        retry_cnt=1,
    )
    tree = parse_one(routed_queries[0], read=ClickHouseDialect)
    tables = list(tree.find_all(exp.Table))
    assert [table.alias for table in tables] == ["l", "r"]
    assert [column.table for column in tree.expressions] == ["l", "r", "l", "r"]


@pytest.mark.parametrize("entrypoint", ["read", "execute_read"])
def test_same_scope_sources_remain_bound_across_cte_shadowing_and_correlation(
    routed_queries: list[str],
    entrypoint: str,
) -> None:
    getattr(sql, entrypoint)(
        "ch",
        "WITH events AS (SELECT id FROM c.events) "
        "SELECT a.events.id,b.events.id FROM a.events JOIN b.events "
        "ON a.events.id=b.events.id WHERE EXISTS "
        "(SELECT 1 FROM events WHERE events.id=a.events.id)",
        retry_cnt=1,
    )
    tree = parse_one(routed_queries[0], read=ClickHouseDialect)
    sources = {
        table.this.expressions[1].this: table.alias
        for table in tree.find_all(exp.Table)
        if isinstance(table.this, exp.Anonymous)
    }
    assert set(sources) == {"a", "b", "c"}
    assert len(set(sources.values())) == 3
    assert tree.expressions[0].table == sources["a"]
    assert tree.expressions[1].table == sources["b"]
    inner = tree.find(exp.Exists).this
    assert inner.args["from_"].this.name == "events"
    assert inner.args["where"].this.left.table == "events"
    assert inner.args["where"].this.right.table == sources["a"]


def test_ambiguous_bare_qualifier_is_not_guessed_between_distinct_local_sources(
    routed_queries: list[str],
) -> None:
    sql.read(
        "ch",
        "SELECT events.id FROM a.events JOIN b.events ON a.events.id=b.events.id",
        retry_cnt=1,
    )
    tree = parse_one(routed_queries[0], read=ClickHouseDialect)
    assert tree.expressions[0].table == "events"
    assert all(table.alias != "events" for table in tree.find_all(exp.Table))


def test_unsupported_catalog_identity_still_fails_before_driver_submission(
    routed_queries: list[str],
) -> None:
    with pytest.raises(InvalidSqlInputError, match="catalog-qualified"):
        sql.read("ch", "SELECT catalog.a.events.id FROM catalog.a.events", retry_cnt=1)
    assert routed_queries == []
