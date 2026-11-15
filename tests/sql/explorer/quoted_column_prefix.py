"""Column completion retains whitespace belonging to a quoted identifier."""

from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace

import pytest
import sqlglot
from analytics_toolkit.sql.execution.cancellation import current_cancellation_scope
from analytics_toolkit.sql.metadata import column_names
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.completion import (
    CompletionCoordinator,
    parse_completion_context,
)
from analytics_toolkit.sql_explorer.identifier_completion import (
    column_completion_matches,
    completion_identifier_parts,
    quote_completion_name,
)

from tests.sql._support.explorer import FakeSession


def _assert_metadata_probe(opened, queries, markers, closed):
    assert opened == ["gp"]
    assert len(queries) == 1
    metadata_select = sqlglot.parse_one(queries[0], read="postgres")
    assert metadata_select.sql(dialect="postgres", comments=False) == (
        'SELECT * FROM "public"."t" LIMIT 1'
    )
    assert [
        comment.strip() for node in metadata_select.walk() for comment in node.comments or ()
    ] == [f"analytics_toolkit {markers[0]}"]
    assert closed == ["cursor", "connection"]


def _column_completion_text(column_name, closed_quote, typed_fragment):
    rendered = quote_completion_name(column_name, "gp")
    fragment = (
        typed_fragment
        if typed_fragment is not None
        else rendered
        if closed_quote
        else rendered[:-1]
    )
    typed_prefix = f"SELECT {fragment}"
    expected_prefix = f"SELECT {rendered}"
    return typed_prefix, typed_prefix + " FROM t", expected_prefix, expected_prefix + " FROM t"


def _complete_quoted_column(
    monkeypatch,
    warm_cache,
    column_name,
    *,
    closed_quote=True,
    typed_fragment=None,
):
    unique_primer = typed_fragment is not None or column_name.casefold().startswith("i")
    fetched = Event()
    opened = []
    queries = []
    markers = []
    closed = []

    def probe(query):
        scope = current_cancellation_scope()
        assert scope is not None
        markers.append(scope.marker)
        queries.append(query)
        fetched.set()

    def connect(connection_key):
        opened.append(connection_key)
        cursor = SimpleNamespace(
            description=[(column_name,), ("id",)] + ([("warm_only",)] if unique_primer else []),
            execute=probe,
            fetchall=list,
            close=lambda: closed.append("cursor"),
        )
        return SimpleNamespace(cursor=lambda: cursor, close=lambda: closed.append("connection"))

    monkeypatch.setattr(column_names, "get_sql_connection", connect)

    async def exercise():
        session = FakeSession()
        app = SqlExplorerApp(session)
        async with app.run_test() as pilot:
            workspace = app.active_workspace
            editor = workspace.editor
            assert isinstance(workspace.completion, CompletionCoordinator)
            editor.focus()
            if warm_cache:
                primer = "warm" if unique_primer else "i"
                editor.text = f"SELECT {primer} FROM t"
                editor.cursor_location = (0, len(f"SELECT {primer}"))
                await pilot.press("tab")
                assert fetched.wait(2)
                await pilot.pause()
                assert editor.text == (
                    "SELECT warm_only FROM t" if unique_primer else "SELECT id FROM t"
                )

            typed_prefix, original, expected_prefix, expected = _column_completion_text(
                column_name, closed_quote, typed_fragment
            )
            editor.text = original
            editor.cursor_location = (0, len(typed_prefix))
            request_kind = app._completion_at_cursor().request.kind

            await pilot.press("tab")
            if request_kind != "column":
                assert editor.text == expected
            assert fetched.wait(2)
            await pilot.pause()

            assert editor.text == expected
            parsed = sqlglot.parse_one(editor.text, read="postgres")
            assert parsed.expressions[0].name == column_name
            assert editor.cursor_location == (0, len(expected_prefix))
            assert not workspace.completion_menu.is_open
            _assert_metadata_probe(opened, queries, markers, closed)
            assert session.executed == []

    asyncio.run(exercise())


@pytest.mark.parametrize("warm_cache", [False, True], ids=["metadata-result", "cached-result"])
def test_quoted_column_prefix_completion_preserves_the_leading_space_column(
    monkeypatch, warm_cache
):
    _complete_quoted_column(monkeypatch, warm_cache, " id")


@pytest.mark.parametrize("column_name", ["where", "from", "select"])
@pytest.mark.parametrize("warm_cache", [False, True], ids=["metadata-result", "cached-result"])
def test_quoted_column_prefix_reserved_names_remain_columns_after_completion(
    monkeypatch, warm_cache, column_name
):
    _complete_quoted_column(monkeypatch, warm_cache, column_name)


def test_quoted_column_prefix_unfinished_identifier_keeps_its_trailing_space(monkeypatch):
    _complete_quoted_column(monkeypatch, False, "id ", closed_quote=False)


@pytest.mark.parametrize(
    ("column_name", "typed_fragment"),
    [('"id', '"""'), ('id"', None), ("`id", '"`'), ("id`", None)],
    ids=["leading-quote", "trailing-quote", "leading-backtick", "trailing-backtick"],
)
@pytest.mark.parametrize("warm_cache", [False, True], ids=["metadata-result", "cached-result"])
def test_quoted_column_prefix_escaped_delimiters_select_only_the_exact_column(
    monkeypatch, warm_cache, column_name, typed_fragment
):
    _complete_quoted_column(
        monkeypatch,
        warm_cache,
        column_name,
        typed_fragment=typed_fragment,
    )


@pytest.mark.parametrize(
    ("query", "prefix"),
    [
        ('SELECT " id"| FROM t', " id"),
        ('SELECT "id "| FROM t', "id "),
        ('SELECT " id "| FROM t', " id "),
        ("SELECT\n  id| FROM t", "id"),
    ],
)
def test_quoted_column_prefix_context_separates_identifier_spaces_from_sql_spacing(query, prefix):
    cursor = query.index("|")
    text = query.replace("|", "")

    context = parse_completion_context(text, cursor, backend="gp", connection_key="gp")

    assert context.request.kind == "column"
    assert context.request.prefix == prefix
    assert context.normalized_prefix == prefix
    assert text[: context.replacement_start] + text[context.replacement_end :] in {
        "SELECT  FROM t",
        "SELECT\n   FROM t",
    }


@pytest.mark.parametrize(("backend", "quote"), [("gp", '"'), ("trino", '"'), ("ch", "`")])
@pytest.mark.parametrize("name", ["where", "from", "select"])
@pytest.mark.parametrize("closed_quote", [True, False], ids=["closed", "unfinished"])
def test_quoted_column_prefix_reserved_identifiers_use_the_column_scope(
    backend, quote, name, closed_quote
):
    prefix = f"SELECT {quote}{name}" + (quote if closed_quote else "")
    text = prefix + " FROM t"

    context = parse_completion_context(text, len(prefix), backend=backend)

    assert context.request.kind == "column"
    assert context.request.prefix == name
    assert context.replacement_start == len("SELECT ")
    assert text[context.replacement_end :] == " FROM t"


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize("keyword", ["where", "from", "select"])
def test_quoted_column_prefix_unquoted_keywords_keep_keyword_completion(backend, keyword):
    prefix = f"SELECT {keyword}"

    context = parse_completion_context(prefix + " FROM t", len(prefix), backend=backend)

    assert context.request.kind == "keyword"
    assert context.request.prefix == keyword


@pytest.mark.parametrize("fragment", ["'unfinished", "id extra", "sum(id)"])
def test_quoted_column_prefix_non_identifier_fragments_retain_their_raw_identity(fragment):
    assert completion_identifier_parts(fragment, "gp") == (fragment,)
    assert column_completion_matches(fragment, fragment, "gp")
    assert not column_completion_matches(fragment, "extra", "gp")


@pytest.mark.parametrize(
    ("backend", "fragment", "decoded"),
    [
        ("gp", '"""', '"'),
        ("trino", '"""', '"'),
        ("ch", "```", "`"),
        ("gp", '"id"""', 'id"'),
        ("ch", "`id```", "id`"),
        ("gp", '"a""b"', 'a"b'),
        ("ch", "`a``b`", "a`b"),
    ],
)
def test_quoted_column_prefix_decodes_literal_delimiters_in_the_identifier(
    backend, fragment, decoded
):
    prefix = f"SELECT {fragment}"

    context = parse_completion_context(prefix + " FROM t", len(prefix), backend=backend)

    assert context.request.kind == "column"
    assert context.request.prefix == decoded
    assert context.replacement_start == len("SELECT ")
    assert context.replacement_end == len(prefix)


@pytest.mark.parametrize(("backend", "quote"), [("gp", '"'), ("trino", '"'), ("ch", "`")])
def test_quoted_column_prefix_keeps_a_dot_inside_one_identifier(backend, quote):
    prefix = f"SELECT {quote}source.column{quote}"

    context = parse_completion_context(prefix + " FROM t", len(prefix), backend=backend)

    assert context.request.kind == "column"
    assert context.request.prefix == "source.column"
    assert context.replacement_start == len("SELECT ")
    assert context.replacement_end == len(prefix)
