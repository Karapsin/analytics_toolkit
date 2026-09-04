from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pandas as pd
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.scrollbars import LeftVerticalScrollbarMixin
from analytics_toolkit.sql_explorer.tabs import NewTabButton, TabSelectButton, WorkspaceTab
from analytics_toolkit.sql_explorer.widgets import FindReplaceBar, ResultTable
from textual.containers import Vertical
from textual.geometry import Region, Size

from tests.sql.explorer.app import FakeSession


def test_workspace_panes_touch_and_main_scrollbars_are_on_the_left() -> None:
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
            assert query_pane.region.bottom == result_pane.region.y
            assert result_pane.region.bottom == command_pane.region.y

            editor = workspace.editor
            table = workspace.query_one(ResultTable)
            for widget in (editor, table):
                assert widget.show_vertical_scrollbar is True
                assert widget.vertical_scrollbar.region.x == widget.content_region.x
                assert widget.show_horizontal_scrollbar is True
                assert widget.horizontal_scrollbar.region.x > widget.content_region.x
                assert widget.scrollbar_corner.region.x == widget.content_region.x

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

            active_tab = application.query_one(WorkspaceTab)
            active_button = active_tab.query_one(TabSelectButton)
            new_tab = application.query_one(NewTabButton)
            assert active_button.styles.background != new_tab.styles.background

            summary = workspace.query_one("#query-summary")
            interrupt = workspace.query_one("#interrupt")
            assert interrupt.region.right == summary.content_region.right

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


def test_left_scrollbar_mixin_handles_each_scrollbar_configuration() -> None:
    class ScrollbarHarness(LeftVerticalScrollbarMixin):
        def __init__(self, vertical: bool, horizontal: bool, *, stable: bool = False) -> None:
            self.scrollbars_enabled = (vertical, horizontal)
            self.scrollbar_size_vertical = 1
            self.scrollbar_size_horizontal = 1
            self.styles = SimpleNamespace(
                scrollbar_size_vertical=1,
                scrollbar_size_horizontal=1,
                scrollbar_gutter="stable" if stable else "auto",
            )
            self.virtual_size = Size(100, 80)
            self.vertical_scrollbar = SimpleNamespace()
            self.horizontal_scrollbar = SimpleNamespace()
            self.scrollbar_corner = SimpleNamespace()

    region = Region(0, 0, 10, 5)
    neither = ScrollbarHarness(False, False)
    assert neither._get_scrollable_region(region) == region
    assert list(neither._arrange_scrollbars(region)) == []

    vertical = ScrollbarHarness(True, False)
    assert vertical._get_scrollable_region(region) == Region(1, 0, 9, 5)
    assert list(vertical._arrange_scrollbars(region)) == [
        (vertical.vertical_scrollbar, Region(0, 0, 1, 5))
    ]

    horizontal = ScrollbarHarness(False, True)
    assert horizontal._get_scrollable_region(region) == Region(0, 0, 10, 4)
    assert list(horizontal._arrange_scrollbars(region)) == [
        (horizontal.horizontal_scrollbar, Region(0, 4, 10, 1))
    ]

    both = ScrollbarHarness(True, True)
    assert both._get_scrollable_region(region) == Region(1, 0, 9, 4)
    assert [widget for widget, _ in both._arrange_scrollbars(region)] == [
        both.scrollbar_corner,
        both.vertical_scrollbar,
        both.horizontal_scrollbar,
    ]
    stable = ScrollbarHarness(False, False, stable=True)
    assert stable._get_scrollable_region(region) == Region(1, 0, 9, 5)

    assert list(vertical._arrange_scrollbars(Region(0, 0, 0, 0))) == []
    assert list(horizontal._arrange_scrollbars(Region(0, 0, 0, 0))) == []
    assert list(both._arrange_scrollbars(Region(0, 0, 0, 0))) == [
        (both.scrollbar_corner, Region(0, -1, 1, 1))
    ]
