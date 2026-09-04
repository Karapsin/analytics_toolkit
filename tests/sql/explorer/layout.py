from __future__ import annotations

import asyncio

import pandas as pd
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.tabs import NewTabButton, TabSelectButton, WorkspaceTab
from analytics_toolkit.sql_explorer.widgets import FindReplaceBar, ResultTable
from textual.color import Color
from textual.containers import Vertical

from tests.sql.explorer.app import FakeSession


def test_workspace_panes_have_one_row_gaps_and_scrollbars_are_on_the_right() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(140, 45)) as pilot:
            workspace = application.active_workspace
            workspace.editor.text = "\n".join(
                f"select {index} AS {'x' * 160}" for index in range(80)
            )
            application.show_dataframe(
                pd.DataFrame({"wide_column": ["x" * 160 for _ in range(100)]})
            )
            await pilot.pause()

            query_pane = workspace.query_one(".query-pane", Vertical)
            result_pane = workspace.query_one(".result-pane", Vertical)
            command_pane = workspace.query_one(".command-panel", Vertical)
            assert result_pane.region.y - query_pane.region.bottom == 1
            assert command_pane.region.y - result_pane.region.bottom == 1

            editor = workspace.editor
            table = workspace.query_one(ResultTable)
            for widget in (editor, table):
                assert widget.show_vertical_scrollbar is True
                assert widget.vertical_scrollbar.region.right == widget.content_region.right
                assert widget.show_horizontal_scrollbar is True
                assert widget.horizontal_scrollbar.region.x == widget.content_region.x
                assert widget.scrollbar_corner.region.right == widget.content_region.right

    asyncio.run(exercise())


def test_editor_position_active_tab_and_summary_control_alignment() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(120, 40)) as pilot:
            workspace = application.active_workspace
            workspace.editor.text = "one\ntwo words"
            workspace.editor.move_cursor((1, 4))
            await pilot.pause()
            assert "SQL  Ln 2, Col 5" in str(workspace.query_one("#editor-status").render())

            editor = workspace.editor
            editor_status = workspace.query_one("#editor-status")
            assert editor.region.bottom == editor_status.region.y
            assert editor.content_region.height >= 2
            assert editor.scroll_offset.y == 0

            active_tab = application.query_one(WorkspaceTab)
            active_button = active_tab.query_one(TabSelectButton)
            new_tab = application.query_one(NewTabButton)
            assert active_button.styles.background != new_tab.styles.background
            assert active_button.styles.background == Color.parse("#D78900")
            assert active_button.styles.color == Color.parse("#0E1113")
            assert active_button.region.height == 1
            assert active_tab.region.height == 1
            assert "[gp] Untitled 1" in str(active_button.label)

            summary = workspace.query_one("#query-summary")
            interrupt = workspace.query_one("#interrupt")
            assert interrupt.region.right == summary.content_region.right

            colors = application.get_css_variables()
            assert colors["background"] == "#0E1113"
            assert colors["panel"] == "#20252A"
            assert colors["accent"] == "#D78900"
            assert workspace.query_one(".command-panel").styles.background == Color.parse("#20252A")

    asyncio.run(exercise())


def test_narrow_editor_keeps_lines_one_and_two_visible_without_status_overlap() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(55, 23)) as pilot:
            workspace = application.active_workspace
            workspace.editor.text = "select 1;\nselect 2;\nselect 3;"
            workspace.editor.move_cursor((0, 0))
            await pilot.pause()

            editor = workspace.editor
            editor_status = workspace.query_one("#editor-status")
            assert editor.region.bottom == editor_status.region.y
            assert editor.content_region.height >= 2
            assert editor.scroll_offset.y == 0
            assert editor.document.get_line(0) == "select 1;"
            assert editor.document.get_line(1) == "select 2;"

    asyncio.run(exercise())


def test_find_replace_panel_uses_target_order_and_floats_at_upper_right() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(160, 45)) as pilot:
            application.action_open_find()
            await pilot.pause()
            panel = application.query_one(FindReplaceBar)
            assert [child.id for child in panel.children] == [
                "find-pattern",
                "replace-pattern",
                "find-next",
                "replace-actions",
            ]
            query_pane = application.active_workspace.query_one(".query-pane", Vertical)
            assert panel.region.width == 80
            assert panel.region.right < query_pane.content_region.right
            assert panel.region.y > query_pane.content_region.y

    asyncio.run(exercise())
