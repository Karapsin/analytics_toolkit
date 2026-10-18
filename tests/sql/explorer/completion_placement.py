from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from agent_tools.sql_explorer_visual_scene import _completion_geometry_assertions
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.completion import CompletionResult
from analytics_toolkit.sql_explorer.completion_commands import SqlExplorerCompletionCommandsMixin
from rich.cells import cell_len
from textual.geometry import Region

from tests.sql._support.completion import _install_stub
from tests.sql._support.explorer import FakeSession

if TYPE_CHECKING:
    from collections.abc import Callable

    from analytics_toolkit.sql_explorer.workspace import SqlExplorerWorkspace


def _assert_popup_rectangle(workspace: SqlExplorerWorkspace) -> None:
    """Derive expected cells from document text, independently of placement code."""
    editor, menu = workspace.editor, workspace.completion_menu
    assert menu.parent is not None
    parent = workspace.query_one(".query-pane").content_region
    viewport = editor.scrollable_content_region
    left, right = max(parent.x, viewport.x), min(parent.right, viewport.right)
    top, bottom = max(parent.y, viewport.y), min(parent.bottom, viewport.bottom)
    row, column = editor.cursor_location
    row_y = viewport.y + row - int(editor.scroll_y)
    caret_x = (
        viewport.x
        + editor.gutter_width
        + cell_len(editor.document[row][:column].expandtabs(editor.indent_width))
        - int(editor.scroll_x)
    )
    wanted = min(12, len(menu.suggestions) + 2)
    above, below = row_y - top, bottom - row_y - 1
    if below >= wanted:
        height, menu_y = wanted, row_y + 1
    elif above >= wanted:
        height, menu_y = wanted, row_y - wanted
    elif below >= above:
        height, menu_y = below, row_y + 1
    else:
        height, menu_y = above, top
    width = min(32, right - left)
    assert menu.is_open
    assert menu.region == Region(max(left, min(caret_x, right - width)), menu_y, width, height)


def _assert_caret_visibility(workspace: SqlExplorerWorkspace, *, visible: bool) -> None:
    editor = workspace.editor
    viewport = editor.scrollable_content_region
    row = viewport.y + editor.cursor_render_offset[1] - int(editor.scroll_y)
    assert (viewport.y <= row < viewport.bottom) is visible, (row, viewport)


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
            _assert_popup_rectangle(workspace)
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
            for _attempt in range(3):
                app._position_completion(workspace)
                app._schedule_completion_position(workspace)
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
            _assert_popup_rectangle(workspace)
            editor.scroll_to(x=10, y=36, animate=False)
            await pilot.pause()
            assert all(_completion_geometry_assertions(workspace).values())
            _assert_popup_rectangle(workspace)
            # Width changes retain a live anchor and must reposition the menu.
            await pilot.resize_terminal(60, 30)
            await pilot.pause()
            _assert_caret_visibility(workspace, visible=True)
            assert all(_completion_geometry_assertions(workspace).values())
            _assert_popup_rectangle(workspace)
            # Shrinking height leaves the old scroll offset intact. An offscreen
            # anchor must close rather than leave a detached completion overlay.
            await pilot.resize_terminal(60, 24)
            await pilot.pause()
            _assert_caret_visibility(workspace, visible=False)
            assert not workspace.completion_menu.is_open
            editor.scroll_cursor_visible(animate=False)
            await pilot.pause()
            await pilot.press("ctrl+space")
            await pilot.pause()
            assert all(_completion_geometry_assertions(workspace).values())
            _assert_popup_rectangle(workspace)
            assert (
                workspace.completion_menu.region.right
                <= workspace.query_one(".query-pane").content_region.right
            )
            workspace.completion_candidates = stub.table_cache
            await pilot.press("_", "n", "a")
            await pilot.pause()
            assert workspace.completion_menu.suggestions == ("customer_name",)
            assert workspace.completion_menu.region.height == 3
            assert all(_completion_geometry_assertions(workspace).values())
            _assert_popup_rectangle(workspace)
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
            _assert_popup_rectangle(workspace)
            menu.action_close()
            workspace.query_one(".query-pane").styles.height = 13
            await pilot.pause()
            editor.scroll_to(y=40 - editor.scrollable_content_region.height // 2, animate=False)
            await pilot.press("ctrl+space")
            await pilot.pause()
            assert menu.is_open
            assert menu.region.height < 12
            assert all(_completion_geometry_assertions(workspace).values())
            _assert_popup_rectangle(workspace)
            menu.highlighted = len(menu.suggestions) - 1
            await pilot.pause()
            assert menu.scroll_y > 0

    asyncio.run(exercise())


def test_async_completion_and_deferred_layout_cannot_restore_cancelled_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            _assert_popup_rectangle(workspace)
            pending: list[Callable[[], None]] = []
            with monkeypatch.context() as patch:
                patch.setattr(app, "call_after_refresh", pending.append)
                app._schedule_completion_position(workspace)
            assert len(pending) == 1
            workspace.completion_menu.action_close()
            pending[0]()
            await pilot.press("ctrl+t")
            await pilot.pause()
            other = app.active_workspace
            assert other is not workspace
            pending[0]()
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
            assert menu.region.right == min(
                workspace.query_one(".query-pane").content_region.right,
                editor.scrollable_content_region.right,
            )
            assert all(_completion_geometry_assertions(workspace).values())
            _assert_popup_rectangle(workspace)
            workspace.query_one(".query-pane").styles.height = 6
            await pilot.pause()
            assert not menu.is_open
            assert editor.text.endswith("* from sample")

    asyncio.run(exercise())


@pytest.mark.parametrize("unmount", [False, True])
def test_deferred_completion_layout_ignores_inactive_workspace(
    monkeypatch: pytest.MonkeyPatch, unmount: bool
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(100, 30)) as pilot:
            stub = _install_stub(app)
            stub.table_cache = ("sample_one", "sample_two")
            workspace = app.active_workspace
            workspace.editor.text = "select * from sample"
            workspace.editor.cursor_location = (0, len(workspace.editor.text))
            await pilot.press("ctrl+space")
            await pilot.pause()
            _assert_popup_rectangle(workspace)
            pending: list[Callable[[], None]] = []
            with monkeypatch.context() as patch:
                patch.setattr(app, "call_after_refresh", pending.append)
                app._schedule_completion_position(workspace)
            assert len(pending) == 1
            await pilot.press("ctrl+t")
            await pilot.pause()
            other = app.active_workspace
            if unmount:
                app._remove_workspace(workspace.tab_id)
                await pilot.pause()
                assert not workspace.is_attached
                assert workspace.tab_id not in app._workspaces
                assert workspace not in app.query(type(workspace))
            # A stale callback must return before looking up or placing its menu.
            pending[0]()
            app._position_completion(workspace)
            app._schedule_completion_position(workspace)
            assert app.active_workspace is other
            assert not other.completion_menu.is_open
            assert other.editor.text == ""

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("target", "attribute", "value"),
    [
        ("app", "_exit_requested", True),
        ("app", "active_workspace", None),
        ("app", "screen_stack", (None, None)),
        ("workspace", "is_mounted", False),
        ("workspace", "completion_epoch", 2),
        ("menu", "is_mounted", False),
        ("menu", "is_open", False),
        ("menu", "suggestions", ("replacement",)),
    ],
)
def test_deferred_position_rejects_stale_menu_state(
    target: str, attribute: str, value: object
) -> None:
    pending: list[Callable[[], None]] = []
    placed: list[object] = []
    menu = SimpleNamespace(is_mounted=True, is_open=True, suggestions=("one", "two"))
    workspace = SimpleNamespace(is_mounted=True, completion_epoch=1, completion_menu=menu)
    app = SimpleNamespace(
        _exit_requested=False,
        active_workspace=workspace,
        screen_stack=(None,),
        call_after_refresh=pending.append,
        _position_completion=placed.append,
    )
    schedule = SqlExplorerCompletionCommandsMixin._schedule_completion_position
    schedule(app, workspace)
    assert len(pending) == 1
    pending.pop()()
    assert placed == [workspace]
    schedule(app, workspace)
    assert len(pending) == 1
    setattr({"app": app, "workspace": workspace, "menu": menu}[target], attribute, value)
    pending.pop()()
    assert placed == [workspace]


def test_modal_screen_prevents_completion_layout_scheduling() -> None:
    pending: list[Callable[[], None]] = []
    workspace = SimpleNamespace(is_mounted=True)
    app = SimpleNamespace(
        _exit_requested=False,
        active_workspace=workspace,
        screen_stack=(None, None),
        call_after_refresh=pending.append,
    )
    # The modal guard must return before looking up the underlying menu.
    SqlExplorerCompletionCommandsMixin._schedule_completion_position(app, workspace)
    assert pending == []
