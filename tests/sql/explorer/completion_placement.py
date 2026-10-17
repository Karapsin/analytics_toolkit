from __future__ import annotations

import asyncio

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.completion import CompletionResult
from rich.cells import cell_len

from agent_tools.sql_explorer_visual_scene import _completion_geometry_assertions
from tests.sql._support.completion import _install_stub
from tests.sql._support.explorer import FakeSession


@pytest.mark.parametrize(
    "query",
    [
        "select c.c| from customers c",
        "with recent as (select 1), recent_orders as (select 2)\nselect *\nfrom rec|",
        "select *\nfrom customers c\nwhere c.c|",
        "select\t'界' as label, c.c| from customers c",
    ],
)
@pytest.mark.parametrize("accept", ["enter", "tab"])
def test_completion_keeps_statement_readable_and_inserts(query: str, accept: str) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(100, 30)) as pilot:
            stub = _install_stub(app)
            stub.table_cache = ("customer_id", "customer_name")
            editor = app.active_workspace.editor
            cursor = query.index("|")
            editor.text = query.replace("|", "")
            editor.cursor_location = app._offset_to_location(editor.text, cursor)
            await pilot.press("ctrl+space")
            await pilot.pause()
            workspace = app.active_workspace
            assert all(_completion_geometry_assertions(workspace).values())
            menu = workspace.completion_menu
            row, column = editor.cursor_location
            prefix = editor.document[row][:column]
            # Calculate the expected cells independently of cursor_render_offset
            # and the harness guard, including the tab stop before the wide glyph.
            expected_x = (
                editor.scrollable_content_region.x
                + editor.gutter_width
                + cell_len(prefix.expandtabs(editor.indent_width))
            )
            assert menu.region.x == expected_x
            assert menu.region.y == editor.scrollable_content_region.y + row + 1
            region = menu.region
            app._position_completion(workspace)
            await pilot.pause()
            assert menu.region == region
            selected = workspace.completion_menu.selected_suggestion()
            context = app._completion_at_cursor()
            before = editor.text[: context.replacement_start]
            after = editor.text[context.replacement_end :]
            await pilot.press(accept)
            assert selected is not None
            assert editor.text == before + selected + ("" if after.startswith(" ") else " ") + after
            assert not workspace.completion_menu.is_open

    asyncio.run(exercise())


def test_completion_repositions_on_scroll_resize_and_narrowing() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(100, 30)) as pilot:
            stub = _install_stub(app)
            stub.table_cache = ("customer_id", "customer_name", "customer_number")
            workspace = app.active_workspace
            editor = workspace.editor
            editor.text = "\n".join(["-- " + "x" * 140] * 40 + ["select * from customer"])
            editor.cursor_location = (40, len("select * from customer"))
            await pilot.pause()
            editor.scroll_to(x=5, y=35, animate=False)
            await pilot.press("ctrl+space")
            await pilot.pause()
            assert editor.scroll_x > 0
            assert editor.scroll_y > 0
            assert all(_completion_geometry_assertions(workspace).values())
            editor.scroll_to(x=10, y=36, animate=False)
            await pilot.pause()
            assert all(_completion_geometry_assertions(workspace).values())
            await pilot.resize_terminal(60, 24)
            await pilot.pause()
            assert all(_completion_geometry_assertions(workspace).values())
            assert workspace.completion_menu.region.right <= editor.parent.content_region.right
            workspace.completion_candidates = stub.table_cache
            await pilot.press("_", "n", "a")
            await pilot.pause()
            assert workspace.completion_menu.suggestions == ("customer_name",)
            assert workspace.completion_menu.region.height == 3
            assert all(_completion_geometry_assertions(workspace).values())
            await pilot.press("escape")
            await pilot.resize_terminal(90, 28)
            await pilot.pause()
            assert not workspace.completion_menu.is_open

    asyncio.run(exercise())


def test_completion_bottom_fallback_and_constrained_option_scrolling() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            stub = _install_stub(app)
            stub.table_cache = tuple(f"sample_{index:02}" for index in range(30))
            workspace = app.active_workspace
            editor = workspace.editor
            editor.text = "\n".join(
                ["-- earlier"] * 40 + ["select * from sample"] + ["-- later"] * 30
            )
            editor.cursor_location = (40, len("select * from sample"))
            await pilot.pause()
            editor.scroll_to(y=41 - editor.scrollable_content_region.height, animate=False)
            await pilot.press("ctrl+space")
            await pilot.pause()
            menu = workspace.completion_menu
            row = editor.scrollable_content_region.y + 40 - int(editor.scroll_y)
            assert menu.region.bottom <= row
            assert all(_completion_geometry_assertions(workspace).values())
            menu.action_close()
            workspace.query_one(".query-pane").styles.height = 13
            await pilot.pause()
            editor.scroll_to(y=40 - editor.scrollable_content_region.height // 2, animate=False)
            await pilot.press("ctrl+space")
            await pilot.pause()
            assert menu.is_open
            assert menu.region.height < 12
            assert all(_completion_geometry_assertions(workspace).values())
            menu.highlighted = len(menu.suggestions) - 1
            await pilot.pause()
            assert menu.scroll_y > 0

    asyncio.run(exercise())


def test_async_completion_and_deferred_layout_cannot_restore_cancelled_menu() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(100, 30)) as pilot:
            stub = _install_stub(app)
            workspace = app.active_workspace
            editor = workspace.editor
            editor.text = "select * from sample"
            editor.cursor_location = (0, len(editor.text))
            await pilot.press("ctrl+space")
            request = app._completion_at_cursor().request
            epoch = workspace.completion_epoch
            stub.table_cache = ("sample_one", "sample_two")
            app._receive_completion(CompletionResult(request, 1, stub.table_cache), "1", epoch)
            await pilot.pause()
            assert all(_completion_geometry_assertions(workspace).values())
            app._schedule_completion_position(workspace)
            workspace.completion_menu.action_close()
            await pilot.press("ctrl+t")
            await pilot.pause()
            other = app.active_workspace
            assert other is not workspace
            app._receive_completion(CompletionResult(request, 2, stub.table_cache), "1", epoch)
            app._position_completion(workspace)
            await pilot.pause()
            assert not workspace.completion_menu.is_open
            assert not other.completion_menu.is_open
            assert other.editor.text == ""

    asyncio.run(exercise())


def test_completion_clamps_right_edge_and_closes_without_row_space() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            stub = _install_stub(app)
            stub.table_cache = ("sample_one", "sample_two")
            workspace = app.active_workspace
            editor = workspace.editor
            editor.text = "select " + " " * 45 + "* from sample"
            editor.cursor_location = (0, len(editor.text))
            await pilot.press("ctrl+space")
            await pilot.pause()
            menu = workspace.completion_menu
            assert menu.region.right == menu.parent.content_region.right
            assert all(_completion_geometry_assertions(workspace).values())
            workspace.query_one(".query-pane").styles.height = 6
            await pilot.pause()
            assert not menu.is_open
            assert editor.text.endswith("* from sample")

    asyncio.run(exercise())
