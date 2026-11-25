"""Completion inserts the current exact identity and owns only its own notices."""

from __future__ import annotations

import asyncio

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.metadata_listing import IncompleteMetadataError

from tests.sql._support.completion import _install_stub
from tests.sql._support.explorer import FakeSession


@pytest.mark.parametrize("name", ["archive.", " Events ", "select"])
def test_namespace_completion_keeps_the_entire_raw_metadata_name(name):
    async def exercise():
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            stub = _install_stub(app)
            stub.schemas[None] = (name,)
            stub.table_cache = ()
            workspace = app.active_workspace
            workspace.editor.text = "select * from "
            workspace.editor.cursor_location = (0, len(workspace.editor.text))
            app._request_completion(workspace)
            assert workspace.editor.text == 'select * from "' + name + '".'

    asyncio.run(exercise())


@pytest.mark.parametrize("query", ["SELECT na FROM t", "SELECT * FROM na"])
def test_stale_keyword_menu_cannot_be_accepted_as_a_column_or_table(query):
    async def exercise():
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            _install_stub(app)
            workspace = app.active_workspace
            editor = workspace.editor
            editor.text = "l"
            editor.cursor_location = (0, 1)
            app._request_completion(workspace)
            assert workspace.completion_menu.is_open
            editor.text = query
            editor.cursor_location = (0, query.index("na") + 2)
            app._accept_completion(workspace)
            assert editor.text == query
            assert not workspace.completion_menu.is_open

    asyncio.run(exercise())


def test_contextless_keyword_candidate_cannot_be_used_as_metadata():
    async def exercise():
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            _install_stub(app)
            workspace = app.active_workspace
            workspace.editor.text = "SELECT le FROM t"
            workspace.editor.cursor_location = (0, len("SELECT le"))
            workspace.completion_menu.open(("left join",))
            app._accept_completion(workspace)
            assert workspace.editor.text == "SELECT le FROM t"
            assert not workspace.completion_menu.is_open

    asyncio.run(exercise())


def test_accepted_namespace_edit_does_not_reapply_the_previous_table_span():
    async def exercise():
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            stub = _install_stub(app)
            stub.schemas[None] = ("sales",)
            stub.table_cache = ("sales_orders",)
            workspace = app.active_workspace
            workspace.editor.text = "select * from sa"
            workspace.editor.cursor_location = (0, len(workspace.editor.text))
            app._request_completion(workspace)
            assert workspace.editor.text == "select * from sales."

    asyncio.run(exercise())


def test_ordinary_metadata_failure_notice_clears_after_complete_recovery():
    async def exercise():
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            stub = _install_stub(app)
            workspace = app.active_workspace
            stub.metadata_incomplete = True
            app._receive_metadata_error(workspace.tab_id, ConnectionError("unavailable"))
            assert "unavailable" in str(workspace.query_one("#notice").renderable)
            stub.metadata_incomplete = False
            app._receive_metadata_complete(workspace.tab_id)
            assert str(workspace.query_one("#notice").renderable) == ""

    asyncio.run(exercise())


def test_background_recovery_never_erases_a_newer_unrelated_notice():
    async def exercise():
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            stub = _install_stub(app)
            workspace = app.active_workspace
            stub.metadata_incomplete = True
            app._receive_metadata_error(workspace.tab_id, IncompleteMetadataError("partial"))
            app._set_notice("File saved.", workspace)
            stub.metadata_incomplete = False
            app._receive_metadata_complete(workspace.tab_id)
            assert str(workspace.query_one("#notice").renderable) == "File saved."

    asyncio.run(exercise())
