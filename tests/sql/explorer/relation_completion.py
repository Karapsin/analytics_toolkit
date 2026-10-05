from __future__ import annotations

import asyncio

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.completion import CompletionResult, parse_completion_context

from tests.sql.explorer.app import FakeSession
from tests.sql.explorer.completion import _install_stub


@pytest.mark.parametrize("backend", ["gp", "ch", "trino"])
@pytest.mark.parametrize(
    ("query", "names"),
    [
        ("with c as (select 1 as id) select * from |", ("c",)),
        ("with c as (select 1 as id) select * from\n c|", ("c",)),
        ("with c as (select 1 as id) select * from -- source\n |", ("c",)),
        ("with c as (select 1 as id) select * from t join |", ("c",)),
        ("with c as (select 1 as id), d as (select * from |) select * from d", ("c",)),
        ("with c as (select * from |), d as (select 1 as id) select * from d", ()),
        ("with c as (select 1 as id) select * from (select * from |) s", ("c",)),
        ("select * from (with hidden as (select 1) select * from hidden) s join |", ()),
        ("with c as (select 1) select * from c; select * from |", ()),
        ("with c as (select 1) select * from |; select * from unrelated", ("c",)),
        ("with c as (select 1) select * from | where (", ()),
        (
            "with c as (select 1) select * from "
            "(with c as (select 2), d as (select 3) select * from |) s",
            ("c", "d"),
        ),
        ("broken ( query; with c as (select 1) select * from |", ("c",)),
        ("with c as (select ';' as id) select * from |", ("c",)),
        ("with c as (select 1) select * from public.|", ()),
        ("with c as (select 1) update |", ()),
        ("with c as (select 1) insert into |", ()),
    ],
)
def test_visible_cte_names_are_statement_and_scope_local(backend, query, names) -> None:
    context = parse_completion_context(query.replace("|", ""), query.index("|"), backend=backend)
    assert context.request.kind == "table"
    assert context.local_relations == names


@pytest.mark.parametrize(
    "query",
    [
        "select * from -- comment|",
        "select 'from |'",
        "-- from |",
        "select * from $tag$broken|",
        "select * from 'users'|",
        "select * from +|",
        "select * from (.|",
        ".|",
    ],
)
def test_relation_lookups_do_not_start_in_comments_or_strings(query) -> None:
    context = parse_completion_context(query.replace("|", ""), query.index("|"), backend="gp")
    assert context.request.kind != "table"


def test_quoted_cte_name_preserves_quoting_and_replacement() -> None:
    query = 'with "Recent Orders" as (select 1) select * from "Recent O"'
    context = parse_completion_context(query, len(query), backend="gp")
    assert context.local_relations == ('"Recent Orders"',)
    assert query[context.replacement_start :] == '"Recent O"'


def test_quoted_cte_can_be_filtered_and_accepted_without_metadata() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            _install_stub(app)
            app._completion = None
            editor = app.active_workspace.editor
            query = 'with "Recent Orders" as (select 1) select * from "Recent O"'
            editor.text = query
            editor.cursor_location = (0, len(query))
            await pilot.press("ctrl+space")
            assert editor.text == query[: query.rindex('"Recent O"')] + '"Recent Orders" '

    asyncio.run(exercise())


def test_no_matching_cte_or_metadata_keeps_the_editor_unchanged() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            stub = _install_stub(app)
            stub.table_cache = ()
            editor = app.active_workspace.editor
            query = "with recent as (select 1 as id) select * from unmatched"
            editor.text = query
            editor.cursor_location = (0, len(query))
            await pilot.press("tab")
            assert editor.text == query
            assert not app.active_workspace.completion_menu.is_open

    asyncio.run(exercise())


@pytest.mark.parametrize("backend", ["gp", "ch", "trino"])
@pytest.mark.parametrize("key", ["tab", "ctrl+space"])
def test_alias_dot_waits_for_completion_key_and_preserves_qualifier(backend, key) -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.database.backend = backend
        app = SqlExplorerApp(session)
        async with app.run_test() as pilot:
            stub = _install_stub(app)
            stub.table_cache = ("id", "name")
            editor = app.active_workspace.editor
            query = "select * from public.users as t1 where t1"
            editor.text = query
            editor.cursor_location = (0, len(query))
            await pilot.press(".")
            assert not app.active_workspace.completion_menu.is_open
            await pilot.press(key)
            assert app._completion_context.request.kind == "column"
            assert app.active_workspace.completion_menu.suggestions == ("id", "name")
            await pilot.press("enter")
            assert editor.text == query + ".id "

    asyncio.run(exercise())


@pytest.mark.parametrize("selection", ["recent", "reporting.", "records"])
def test_ctes_tables_and_namespaces_merge_with_correct_insertion(selection) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            stub = _install_stub(app)
            stub.schemas = {None: ("reporting",)}
            stub.table_cache = ("records",)
            editor = app.active_workspace.editor
            query = "with recent as (select 1 as id) select * from re"
            editor.text = query
            editor.cursor_location = (0, len(query))
            await pilot.press("tab")
            menu = app.active_workspace.completion_menu
            assert menu.suggestions == ("recent", "records", "reporting.")
            menu.highlighted = menu.suggestions.index(selection)
            await pilot.press("enter")
            assert editor.text == query[:-2] + selection + ("" if selection.endswith(".") else " ")

    asyncio.run(exercise())


def test_ctes_survive_pending_failed_and_stale_metadata() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            stub = _install_stub(app)
            workspace = app.active_workspace
            editor = workspace.editor
            query = "with recent as (select 1 as id) select * from re"
            editor.text = query
            editor.cursor_location = (0, len(query))
            await pilot.press("ctrl+space")
            assert workspace.completion_menu.suggestions == ("recent",)
            assert editor.text == query
            epoch = workspace.completion_epoch
            request = workspace.completion_context.request
            app._receive_metadata_error(workspace.tab_id, ValueError("offline"), epoch)
            assert workspace.completion_menu.suggestions == ("recent",)
            stub.table_cache = ("records",)
            app._receive_completion(
                CompletionResult(request, 1, ("records",)), workspace.tab_id, epoch
            )
            assert workspace.completion_menu.suggestions == ("recent", "records")
            assert editor.text == query
            await pilot.press("c")
            assert workspace.completion_menu.suggestions == ("recent", "records")
            await pilot.press("escape")
            app._receive_completion(
                CompletionResult(request, 1, ("records",)), workspace.tab_id, epoch
            )
            assert not workspace.completion_menu.is_open

    asyncio.run(exercise())
