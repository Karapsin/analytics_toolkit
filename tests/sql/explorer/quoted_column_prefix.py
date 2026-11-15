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

from tests.sql._support.explorer import FakeSession


@pytest.mark.parametrize("warm_cache", [False, True], ids=["metadata-result", "cached-result"])
def test_quoted_column_prefix_completion_preserves_the_leading_space_column(
    monkeypatch, warm_cache
):
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
            description=[(" id",), ("id",)],
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
                editor.text = "SELECT i FROM t"
                editor.cursor_location = (0, len("SELECT i"))
                await pilot.press("tab")
                assert fetched.wait(2)
                await pilot.pause()
                assert editor.text == "SELECT id FROM t"

            original = 'SELECT " id" FROM t'
            cursor = len('SELECT " id"')
            editor.text = original
            editor.cursor_location = (0, cursor)

            await pilot.press("tab")
            assert fetched.wait(2)
            await pilot.pause()

            assert editor.text == original
            parsed = sqlglot.parse_one(editor.text, read="postgres")
            assert parsed.expressions[0].name == " id"
            assert editor.cursor_location == (0, cursor)
            assert not workspace.completion_menu.is_open
            assert opened == ["gp"]
            assert len(queries) == 1
            metadata_select = sqlglot.parse_one(queries[0], read="postgres")
            assert metadata_select.sql(dialect="postgres", comments=False) == (
                'SELECT * FROM "public"."t" LIMIT 1'
            )
            assert [
                comment.strip()
                for node in metadata_select.walk()
                for comment in node.comments or ()
            ] == [f"analytics_toolkit {markers[0]}"]
            assert closed == ["cursor", "connection"]
            assert session.executed == []

    asyncio.run(exercise())


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
