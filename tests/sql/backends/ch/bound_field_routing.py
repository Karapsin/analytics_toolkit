"""Routed table qualifiers retain current-database and tuple-field bindings."""

from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.routing import ChClusterRouting, route_sql
from analytics_toolkit.sql.backends.ch.source_binding import _bound_source, _matches_source
from sqlglot import exp, parse_one
from sqlglot.optimizer.scope import traverse_scope

from tests.sql._support.outage import Client, Result


@pytest.mark.parametrize(
    ("projection", "source", "expected"),
    [
        ("db.events.id", "events", "events.id"),
        ("db.events.*", "events", "events.*"),
        ("events.payload.x", "db.events", "events.payload.x"),
        ("db.events.payload.x", "db.events", "events.payload.x"),
        ("e.payload.x", "db.events AS e", "e.payload.x"),
    ],
)
def test_public_read_routes_current_database_and_tuple_field_qualifiers(
    outage: Any, monkeypatch: Any, projection: str, source: str, expected: str
) -> None:
    cluster, _configure = outage
    # The boundary returns data while this regression checks the actual SQL
    # submitted through the public facade. The fixture's simple value projector
    # does not implement tuple access or qualified stars.
    monkeypatch.setattr(
        Client, "_project", staticmethod(lambda _statement, rows, _obj: Result(rows))
    )
    sql.read("ch", f"SELECT {projection} FROM {source}", retry_cnt=1)
    submitted = next(
        query
        for query in reversed(cluster.queries)
        if "cluster(" in query and "'events_shard'" in query
    )
    statement = parse_one(submitted, read=ClickHouseDialect)
    assert statement.expressions[0].sql(dialect=ClickHouseDialect) == expected
    table = next(statement.find_all(exp.Table))
    assert table.alias == ("e" if source.endswith(" AS e") else "events")


def test_tuple_field_binding_does_not_capture_a_shadowed_outer_alias() -> None:
    routed = route_sql(
        "SELECT events.payload.x FROM db.events WHERE EXISTS "
        "(WITH events AS (SELECT payload FROM db.raw) "
        "SELECT events.payload.x FROM events)",
        routing=ChClusterRouting("core"),
        database="db",
    )
    statement = parse_one(routed, read=ClickHouseDialect)
    assert statement.expressions[0].sql(dialect=ClickHouseDialect).endswith(".payload.x")
    nested = list(statement.find_all(exp.Select))[1]
    assert nested.expressions[0].sql(dialect=ClickHouseDialect) == "events.payload.x"
    source = next(statement.find_all(exp.Table))
    assert source.alias.startswith("_atk_source_")


def test_binding_helpers_keep_original_source_identity_with_namespace_defaults() -> None:
    statement = parse_one("SELECT db.events.id FROM events", read=ClickHouseDialect)
    scope = next(iter(traverse_scope(statement)))
    source = next(statement.find_all(exp.Table))
    column = next(statement.find_all(exp.Column))
    assert _matches_source(column, source, "db")
    assert not _matches_source(column, source, "other")
    assert _bound_source(scope, column, {id(scope): [source]}, "db") is source
