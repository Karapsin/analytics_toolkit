"""Editor-local CTEs must not change quoted metadata table identities."""

from __future__ import annotations

import asyncio
from threading import Event

import pandas as pd
import pytest
import sqlglot
from analytics_toolkit.sql.backends.metadata import (
    GP_PARTITION_CAPABILITIES_QUERY,
    build_gp_completion_query,
)
from analytics_toolkit.sql_explorer import completion
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.completion import CompletionCoordinator
from analytics_toolkit.sql_explorer.identifier_completion import quote_completion_name

from tests.sql._support.explorer import FakeSession


def _metadata_reader(monkeypatch, table_name, *, prefix=None, other_name=None):
    fetched = Event()
    table_queries = []
    prefix = table_name[:3] if prefix is None else prefix
    table_query = build_gp_completion_query(None, prefix, "none")
    names = [table_name] if other_name is None else [table_name, other_name]

    def read(connection_key, query, **kwargs):
        assert connection_key == "gp"
        assert kwargs["return_metadata"] is True
        assert kwargs["query_label"].startswith("sql_explorer metadata=")
        if query == GP_PARTITION_CAPABILITIES_QUERY:
            return pd.DataFrame({"partition_catalog": ["none"]})
        if query == "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name":
            return pd.DataFrame({"schema_name": ["public"]})
        assert query == table_query
        assert table_name.startswith(prefix)
        table_queries.append(query)
        fetched.set()
        return pd.DataFrame({"table_name": names})

    monkeypatch.setattr(completion.sql, "read", read)
    return fetched, table_queries


async def _complete_table(table_name, prelude, warm_cache, fetched):
    session = FakeSession()
    app = SqlExplorerApp(session)
    async with app.run_test() as pilot:
        workspace = app.active_workspace
        coordinator = workspace.completion
        assert isinstance(coordinator, CompletionCoordinator)
        editor = workspace.editor
        rendered = quote_completion_name(table_name, "gp")
        fragment = quote_completion_name(table_name[:3], "gp")[:-1]
        before = prelude + "SELECT * FROM "
        after = " WHERE 1 = 1"
        editor.text = before + fragment + after
        editor.cursor_location = (0, len(before + fragment))
        editor.focus()
        context = app._completion_at_cursor(workspace)
        assert context.request.kind == "table"
        assert context.request.prefix == table_name[:3]
        assert bool(context.local_relations) == bool(prelude)

        if warm_cache:
            ready = Event()
            errors = []
            coordinator.enqueue(
                context.request,
                on_success=lambda _result: ready.set(),
                on_error=lambda _result, exc: (errors.append(exc), ready.set()),
            )
            assert ready.wait(2)
            assert errors == []
            assert coordinator.cached(context.request) == (table_name,)
        else:
            assert coordinator.cached(context.request) is None

        await pilot.press("tab")
        assert fetched.wait(2)
        await pilot.pause()
        if workspace.completion_menu.is_open:
            await pilot.press("tab")
            await pilot.pause()

        expected_prefix = before + rendered
        assert editor.text == expected_prefix + after
        parsed = sqlglot.parse_one(editor.text, read="postgres")
        relation = parsed.args.get("from_") or parsed.args.get("from")
        assert relation.this.name == table_name
        assert editor.cursor_location == (0, len(expected_prefix))
        assert not workspace.completion_menu.is_open
        assert session.executed == []


@pytest.mark.parametrize("warm_cache", [False, True], ids=["metadata-result", "cached-result"])
@pytest.mark.parametrize(
    ("table_name", "prelude"),
    [
        ('"table', "WITH seed AS (SELECT 1 AS value) "),
        ('"where"', 'WITH "where" AS (SELECT 1 AS value) '),
    ],
    ids=["leading-literal-quote", "rendered-cte-collision"],
)
def test_quoted_table_prefix_preserves_metadata_identity_with_visible_ctes(
    monkeypatch, warm_cache, table_name, prelude
):
    fetched, table_queries = _metadata_reader(monkeypatch, table_name)

    async def exercise():
        for visible_ctes in ("", prelude):
            fetched.clear()
            await _complete_table(table_name, visible_ctes, warm_cache, fetched)

    asyncio.run(exercise())
    assert len(table_queries) == 2


def test_quoted_table_prefix_live_menu_refresh_retains_rendered_identifier(monkeypatch):
    table_name, other_name = '"tablea', '"tableb'
    prefix = table_name[:-1]
    fetched, table_queries = _metadata_reader(
        monkeypatch, table_name, prefix=prefix, other_name=other_name
    )

    async def exercise():
        session = FakeSession()
        app = SqlExplorerApp(session)
        async with app.run_test() as pilot:
            workspace = app.active_workspace
            editor = workspace.editor
            before = "WITH seed AS (SELECT 1 AS value) SELECT * FROM "
            after = " WHERE 1 = 1"
            fragment = quote_completion_name(prefix, "gp")[:-1]
            editor.text = before + fragment + after
            editor.cursor_location = (0, len(before + fragment))
            editor.focus()

            await pilot.press("tab")
            assert fetched.wait(2)
            await pilot.pause()
            menu = workspace.completion_menu
            assert menu.is_open
            assert menu.suggestions == tuple(
                quote_completion_name(name, "gp") for name in (table_name, other_name)
            )

            await pilot.press("a")
            assert editor.text == before + fragment + "a" + after
            assert menu.suggestions == (quote_completion_name(table_name, "gp"),)
            await pilot.press("tab")

            expected_prefix = before + quote_completion_name(table_name, "gp")
            assert editor.text == expected_prefix + after
            parsed = sqlglot.parse_one(editor.text, read="postgres")
            relation = parsed.args.get("from_") or parsed.args.get("from")
            assert relation.this.name == table_name
            assert editor.cursor_location == (0, len(expected_prefix))
            assert not menu.is_open
            assert session.executed == []

    asyncio.run(exercise())
    assert len(table_queries) == 1
