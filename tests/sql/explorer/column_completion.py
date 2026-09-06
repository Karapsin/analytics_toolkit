from __future__ import annotations

from types import SimpleNamespace

import pytest
import sqlglot
from analytics_toolkit import sql
from analytics_toolkit.sql_explorer.column_completion import (
    _source_columns,
    column_fragment,
    projection_context,
    projection_suggestions,
)
from analytics_toolkit.sql_explorer.completion import (
    CompletionCoordinator,
    parse_completion_context,
)
from sqlglot import exp
from sqlglot.optimizer.scope import Scope

from tests.sql.explorer.completion import FakeProvider


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("select i| from public.users", ("id", "name")),
        ("select u.i| from public.users u", ("id", "name")),
        (
            "select i| from public.users u join orders o on u.id=o.id",
            ("amount", "name", "o.id", "u.id"),
        ),
        (
            "with c as (select id as user_id, name from public.users) select i| from c",
            ("name", "user_id"),
        ),
        ("with c as (select * from public.users) select i| from c", ("id", "name")),
        ("select i| from (select id as user_id from public.users) c", ("user_id",)),
        ("with c(user_id) as (select id from public.users) select i| from c", ("user_id",)),
        ("select id from public.users; select i| from orders", ("amount", "id")),
        ("select (select i| from orders) from public.users", ("amount", "id")),
    ],
)
def test_projection_scope(query: str, expected: tuple[str, ...]) -> None:
    cursor = query.index("|")
    text = query.replace("|", "")
    context = parse_completion_context(text, cursor, backend="gp", connection_key="warehouse")
    assert context.request.kind == "column"
    tables = {"public.users": ("id", "name"), "orders": ("id", "amount")}
    assert projection_suggestions(context.request.context, "gp", tables.__getitem__) == expected


@pytest.mark.parametrize(
    "query",
    [
        "select 'ab|c' from users",
        "select id from users where |",
        "select |",
        "select | from users",
        "select distinct| from users",
        "select id | from users",
        "select na|me from users",
        "select id|, name from users",
        "select id\n| from users",
        "-- select i| from users",
    ],
)
def test_non_projection_does_not_fetch_columns(query: str) -> None:
    context = parse_completion_context(query.replace("|", ""), query.index("|"), backend="gp")
    assert context.request.kind != "column"


@pytest.mark.parametrize(
    ("backend", "query", "column"),
    [
        ("gp", 'select "Full N"| from users', "Full Name"),
        ("ch", "select `Full N`| from users", "Full Name"),
        ("trino", 'select "Full N"| from users', "Full Name"),
        ("gp", "select na| from users", "name"),
    ],
)
def test_column_fragment_replaces_quotes_and_suffix(backend: str, query: str, column: str) -> None:
    text, cursor = query.replace("|", ""), query.index("|")
    context = parse_completion_context(text, cursor, backend=backend)
    assert context.request.kind == "column"
    options = projection_suggestions(context.request.context, backend, lambda _: (column,))
    completed = text[: context.replacement_start] + options[0] + text[context.replacement_end :]

    assert (
        sqlglot.parse_one(
            completed, read={"gp": "postgres", "ch": "clickhouse", "trino": "trino"}[backend]
        )
        .expressions[0]
        .name
        == column
    )


def test_columns_cache_and_ddl_invalidation(monkeypatch: pytest.MonkeyPatch) -> None:

    calls = []

    def metadata(db_key: str, table: str, *, include_row_count: bool) -> SimpleNamespace:
        calls.append((db_key, table, include_row_count))
        return SimpleNamespace(columns={"id": "BIGINT", "name": "TEXT"})

    monkeypatch.setattr(sql, "table_info", metadata)
    coordinator = CompletionCoordinator("warehouse", "gp", provider=FakeProvider())
    try:
        request = parse_completion_context("select i from users", 8, backend="gp").request
        assert coordinator._run(request) == ("id", "name")
        assert coordinator._run(request) == ("id", "name")
        assert calls == [("warehouse", "users", False)]
        coordinator.invalidate_tables()
        coordinator._run(request)
        assert len(calls) == 2
    finally:
        coordinator.stop()


@pytest.mark.parametrize(
    "statement",
    [
        "select x from t",
        "select __explorer_cursor_column__ from (select 1 + 1 from t) c",
        "select __explorer_cursor_column__ from (select t.* from t join u on t.id=u.id) c",
        "with c as (select id from t union all select id from u) "
        "select __explorer_cursor_column__ from c",
    ],
)
def test_derived_projection_outputs(statement: str) -> None:
    options = projection_suggestions(statement, "gp", lambda _: ("id",))
    assert options == (() if statement.startswith("select x") or "1 + 1" in statement else ("id",))


def test_malformed_or_non_projection_context_is_ignored() -> None:

    assert column_fragment("select 'unclosed", 10, "gp") is None
    assert projection_context("select id from t where na", 23, 25, "gp") is None
    assert projection_context("select na", 7, 9, "gp") is None
    assert projection_context("update t set id = na", 18, 20, "gp") is None


def test_recursive_source_terminates_without_metadata_recursion() -> None:

    def fetch(_: str) -> tuple[str, ...]:
        return ("id",)

    assert _source_columns(exp.Literal.number(1), frozenset(), "postgres", fetch) == ()
    scope = Scope(exp.select("id"))
    assert _source_columns(scope, frozenset({id(scope)}), "postgres", fetch) == ()
    assert _source_columns(Scope(exp.Union()), frozenset(), "postgres", fetch) == ()


def test_column_cache_filters_qualified_names_and_ignores_stale_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = CompletionCoordinator("warehouse", "gp", provider=FakeProvider())
    try:
        request = parse_completion_context("select i from users", 8, backend="gp").request
        coordinator._store_result(request, ("u.id", "name", "id_number"))
        assert coordinator.cached(request) == ("u.id", "id_number")
        coordinator._store_result(request, ("stale",), generation=-1)
        assert coordinator.cached(request) == ("u.id", "id_number")

        def metadata(*args: object, **kwargs: object) -> SimpleNamespace:
            coordinator.invalidate_tables()
            return SimpleNamespace(columns={"fresh": "TEXT"})

        monkeypatch.setattr(sql, "table_info", metadata)
        assert coordinator._columns_for_table("users") == ("fresh",)
        assert coordinator._table_columns == {}
    finally:
        coordinator.stop()


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("select | from users", True),
        ("select\n|\nfrom users", True),
        ("select id, | from users", True),
        ("select |name from users", False),
        ("select |", False),
        ("select id from users where |", False),
    ],
)
def test_explicit_empty_column_completion_keeps_other_context_guards(
    query: str, expected: bool
) -> None:
    context = parse_completion_context(
        query.replace("|", ""), query.index("|"), backend="gp", allow_empty_column_prefix=True
    )
    assert (context.request.kind == "column") is expected
