from __future__ import annotations

import asyncio

from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.completion import CompletionRequest, CompletionResult
from textual.widgets import Input

from tests.sql.explorer.app import FakeSession
from tests.sql.explorer.completion import _install_stub


def test_namespace_menu_filters_its_own_candidates_and_shrinks() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            stub = _install_stub(app)
            stub.schemas[None] = ("sales", "sandbox", "scratch")
            stub.table_cache = ()
            editor = app.active_workspace.editor
            editor.text = "select * from s"
            editor.cursor_location = (0, len(editor.text))
            await pilot.press("ctrl+space")
            menu = app.active_workspace.completion_menu
            assert len(menu.suggestions) == 3
            height = menu.styles.height
            await pilot.press("a", "n")
            assert menu.suggestions == ("sandbox",)
            assert menu.styles.height.value < height.value
            assert editor.text.endswith("san")
            await pilot.press("backspace")
            assert menu.suggestions == ("sales", "sandbox")
            await pilot.press("z")
            assert not menu.is_open

    asyncio.run(exercise())


def test_results_close_icon_preserves_sql_and_returns_focus() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            app.active_workspace.editor.text = "select 1"
            app.show_error(ValueError("fixture error"))
            await pilot.pause()
            await pilot.click("#close-results")
            assert not app.active_workspace.results_open
            assert app.active_workspace.editor.text == "select 1"
            assert app.focused is app.active_workspace.editor
            assert app.query_one("#command-input", Input).value == ""

    asyncio.run(exercise())


def test_dismissed_namespace_response_cannot_reopen_menu() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            stub = _install_stub(app)
            stub.schemas[None] = ("sales", "sandbox")
            editor = app.active_workspace.editor
            editor.text = "select * from sa"
            editor.cursor_location = (0, len(editor.text))
            await pilot.press("ctrl+space")
            menu = app.active_workspace.completion_menu
            assert menu.is_open
            await pilot.press("escape")
            app._receive_namespace(
                CompletionResult(CompletionRequest("gp", "gp", "schema", ""), 1, ("sales",))
            )
            assert not menu.is_open
            assert editor.text == "select * from sa"

    asyncio.run(exercise())


def test_multiple_cursors_close_menu_and_tab_only_indents() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            _install_stub(app)
            editor = app.active_workspace.editor
            editor.text = "i\ni"
            editor.cursor_location = (0, 1)
            await pilot.press("tab")
            assert app.active_workspace.completion_menu.is_open
            await pilot.press("shift+down")
            assert editor.cursor_count == 2
            assert not app.active_workspace.completion_menu.is_open
            text = editor.text
            await pilot.press("ctrl+space")
            assert editor.text == text
            assert not app._request_completion()
            await pilot.press("tab")
            assert editor.text.count("    ") == 2

    asyncio.run(exercise())


def test_cursor_return_does_not_reopen_cancelled_metadata_response() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            stub = _install_stub(app)
            editor = app.active_workspace.editor
            editor.text = "select * from sample"
            editor.cursor_location = (0, len(editor.text))
            await pilot.press("tab")
            epoch = app.active_workspace.completion_epoch
            request = app._completion_at_cursor().request
            await pilot.press("left", "right", "tab")
            stub.table_cache = ("sample_one", "sample_two")
            app._receive_completion(CompletionResult(request, 1, stub.table_cache), "1", epoch)
            assert not app.active_workspace.completion_menu.is_open

    asyncio.run(exercise())


def test_metadata_notice_clears_on_finish_cancel_and_preserves_newer_notice() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            stub = _install_stub(app)
            workspace = app.active_workspace
            editor = workspace.editor
            editor.text = "select * from sample"
            editor.cursor_location = (0, len(editor.text))
            await pilot.press("tab")
            notice = workspace.query_one("#notice")
            assert "Loading" in str(notice.render())
            request = app._completion_at_cursor().request
            stub.table_cache = ("sample_one", "sample_two")
            app._completion_from_thread(
                workspace.tab_id,
                CompletionResult(request, 1, stub.table_cache),
                workspace.completion_epoch,
            )
            assert str(notice.renderable) == ""
            workspace.completion_menu.action_close()
            stub.table_cache = None
            await pilot.press("tab", "left")
            assert "Loading" not in str(notice.render())
            await pilot.press("right", "tab")
            epoch = workspace.completion_epoch
            app._set_notice("File saved.", workspace)
            await pilot.press("left")
            app._metadata_error_from_thread(
                workspace.tab_id, CompletionResult(request, 1, ()), ValueError("old error"), epoch
            )
            assert str(notice.renderable) == "File saved."
            await pilot.press("right", "tab")
            app._metadata_error_from_thread(
                workspace.tab_id,
                CompletionResult(request, 2, ()),
                ValueError("metadata failed"),
                workspace.completion_epoch,
            )
            assert "Loading" not in str(notice.render())
            assert "metadata failed" in str(notice.render())

    asyncio.run(exercise())


def test_completion_rejects_other_scopes_and_multicursor_acceptance() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            _install_stub(app)
            workspace = app.active_workspace
            editor = workspace.editor
            editor.text = "i\ni"
            editor.cursor_location = (0, 1)
            await pilot.press("tab", "tab")
            assert editor.text.startswith("inner join")
            editor.text = "select * from sample"
            editor.cursor_location = (0, len(editor.text))
            await pilot.press("tab")
            unrelated = CompletionRequest("other", "gp", "table", "sample")
            app._receive_completion(CompletionResult(unrelated, 1, ("sample_other",)))
            app._receive_namespace(CompletionResult(unrelated, 1, ()))
            assert editor.text == "select * from sample"
            editor.text = "i\ni"
            editor.cursor_location = (0, 1)
            await pilot.press("tab", "shift+down")
            app._accept_completion()
            assert editor.text == "i\ni"
            assert not workspace.completion_menu.is_open

    asyncio.run(exercise())


def test_shift_tab_requests_columns_with_blank_left_cell() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            stub = _install_stub(app)
            stub.table_cache = ("id", "name")
            editor = app.active_workspace.editor
            editor.text = "select  from users"
            editor.cursor_location = (0, 7)
            await pilot.press("shift+tab")
            assert editor.text == "select  from users"
            assert app.active_workspace.completion_menu.suggestions == ("id", "name")
            assert app._completion_at_cursor().request.kind == "column"
            await pilot.press("tab")
            assert editor.text == "select id from users"
            editor.text = "    select 1"
            editor.cursor_location = (0, 4)
            await pilot.press("shift+tab")
            assert editor.text == "select 1"

    asyncio.run(exercise())
