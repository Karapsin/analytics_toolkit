"""Unavailable quoted completion consumes Tab without rewriting SQL identity."""

from __future__ import annotations

import asyncio

import pytest
import sqlglot
from analytics_toolkit.sql.metadata import column_names
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.completion import (
    CompletionCoordinator,
    parse_completion_context,
)
from sqlglot import exp

from tests.sql._support.explorer import FakeSession
from tests.sql._support.quoted_column_metadata import ColumnMetadataProbe


@pytest.mark.parametrize(
    ("query", "quoted_name", "warm_cache", "key"),
    [
        ('SELECT "where"|, id FROM t', "where", False, "tab"),
        ('SELECT "where|",id FROM t', "where", True, "tab"),
        ('SELECT "where|ver" FROM t', "wherever", False, "tab"),
        ('SELECT id AS "where"| FROM t', "where", False, "tab"),
        ('SELECT "where"|', "where", False, "tab"),
        ('SELECT "label|", id FROM t', "label", False, "tab"),
        ('    SELECT "where|",id FROM t', "where", False, "shift+tab"),
    ],
    ids=[
        "comma",
        "paired-quote-warm",
        "inside-token",
        "alias",
        "no-source",
        "ordinary-name",
        "shift-tab",
    ],
)
def test_quoted_column_boundaries_preserve_unavailable_identifier_completion(
    monkeypatch, query, quoted_name, warm_cache, key
):
    probe = ColumnMetadataProbe()
    monkeypatch.setattr(column_names, "get_sql_connection", probe.connect)
    cursor = query.index("|")
    original = query.replace("|", "")

    async def exercise():
        session = FakeSession()
        app = SqlExplorerApp(session)
        async with app.run_test() as pilot:
            workspace = app.active_workspace
            editor = workspace.editor
            assert isinstance(workspace.completion, CompletionCoordinator)
            editor.focus()
            if warm_cache:
                editor.text = "SELECT warm FROM t"
                editor.cursor_location = (0, len("SELECT warm"))
                await pilot.press("tab")
                assert probe.fetched.wait(2)
                await pilot.pause()
                assert editor.text == "SELECT warm_only FROM t"
                probe.assert_finished()
            baseline = probe.snapshot()
            editor.text = original
            editor.cursor_location = (0, cursor)

            await pilot.press(key)
            await pilot.pause()

            assert editor.text == original
            assert editor.cursor_location == (0, cursor)
            parsed = sqlglot.parse_one(editor.text, read="postgres")
            assert [
                identifier.name
                for identifier in parsed.find_all(exp.Identifier)
                if identifier.args.get("quoted")
            ] == [quoted_name]
            assert probe.snapshot() == baseline
            assert not workspace.completion_menu.is_open
            assert session.executed == []

    asyncio.run(exercise())


def test_quoted_column_boundaries_refuse_a_stale_keyword_menu_acceptance(monkeypatch):
    probe = ColumnMetadataProbe()
    monkeypatch.setattr(column_names, "get_sql_connection", probe.connect)

    async def exercise():
        session = FakeSession()
        app = SqlExplorerApp(session)
        async with app.run_test() as pilot:
            workspace = app.active_workspace
            editor = workspace.editor
            editor.text = "l"
            editor.cursor_location = (0, 1)
            editor.focus()
            await pilot.press("tab")
            assert workspace.completion_menu.suggestions == ("left join", "limit")
            assert workspace.completion_menu.is_open

            original = 'SELECT "where",id FROM t'
            cursor = len('SELECT "where')
            editor.text = original
            editor.cursor_location = (0, cursor)
            assert workspace.completion_menu.is_open
            # Deliver the existing widget's accept action before the queued
            # editor-change refresh can discard its previous keyword context.
            workspace.completion_menu.action_accept()
            await pilot.pause()

            assert editor.text == original
            assert editor.cursor_location == (0, cursor)
            assert not workspace.completion_menu.is_open
            assert probe.snapshot() == {"opened": [], "queries": [], "markers": [], "closed": []}
            assert session.executed == []

    asyncio.run(exercise())


@pytest.mark.parametrize("query", ['-- "where"|', 'SELECT \'a "where"|'])
def test_quoted_column_boundaries_do_not_mark_comment_or_string_text_as_identifier(query):
    cursor = query.index("|")

    context = parse_completion_context(query.replace("|", ""), cursor, backend="gp")

    assert context.request.kind == "keyword"
    assert context.raw_prefix.startswith('"')
    assert context.request.prefix == "where"


@pytest.mark.parametrize(
    ("original", "expected"), [("whe", "where "), ("", "    ")], ids=["keyword", "indent"]
)
def test_quoted_column_boundaries_keep_unquoted_keyword_and_indent_behavior(
    monkeypatch, original, expected
):
    probe = ColumnMetadataProbe()
    monkeypatch.setattr(column_names, "get_sql_connection", probe.connect)

    async def exercise():
        session = FakeSession()
        app = SqlExplorerApp(session)
        async with app.run_test() as pilot:
            editor = app.active_workspace.editor
            editor.text = original
            editor.cursor_location = (0, len(original))
            editor.focus()

            await pilot.press("tab")
            await pilot.pause()

            assert editor.text == expected
            assert editor.cursor_location == (0, len(expected))
            assert probe.snapshot() == {"opened": [], "queries": [], "markers": [], "closed": []}
            assert session.executed == []

    asyncio.run(exercise())
