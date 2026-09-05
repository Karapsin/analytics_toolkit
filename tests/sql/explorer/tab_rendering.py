from __future__ import annotations

import asyncio

from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.tabs import TabSelectButton, WorkspaceTab

from tests.sql.explorer.app import FakeSession


def test_new_tab_database_switch_reflows_visible_title() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+t")
            await pilot.pause()
            workspace = application.active_workspace
            tab = application.query_one(f"#tab-{workspace.tab_id}", WorkspaceTab)
            button = tab.query_one(TabSelectButton)
            for alias in ("warehouse_connection", "gp", "another_long_database_alias"):
                application._command_database([alias])
                await pilot.pause()
                expected = f"[{alias}] Untitled 2"
                assert expected in button.render_line(0).text
                assert button.region.right <= tab.region.right
                assert tab.query_one(".tab-close").region.x >= button.region.right
            workspace.editor.text = "select 1"
            await pilot.pause()
            assert "Untitled 2*" in button.render_line(0).text
            await pilot.press("ctrl+tab", "ctrl+tab")
            assert application.active_workspace is workspace
            assert "Untitled 2*" in button.render_line(0).text
            for _ in range(6):
                await pilot.press("ctrl+t")
            await pilot.pause()
            strip = application.query_one("#tab-strip")
            assert strip.max_scroll_x > 0
            assert strip.scrollable_content_region.height >= 1

    asyncio.run(exercise())
