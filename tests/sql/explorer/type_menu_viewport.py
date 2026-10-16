from __future__ import annotations

import asyncio

from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.create_table_screen import CreateTableScreen
from textual.widgets import Collapsible, Input, OptionList

from tests.sql._support.explorer import FakeSession


def test_type_menu_disappears_when_focused_field_scrolls_out_of_view() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 40)) as pilot:
            screen = CreateTableScreen(
                "ch",
                "ch",
                {
                    "table_name": "sandbox.orders",
                    "rows": [("id", "BIGINT"), ("value", "NUMERIC")],
                },
            )
            await app.push_screen(screen)
            screen.query_one(Collapsible).collapsed = False
            field = screen.query(".create-column-type").first(Input)
            field.focus()
            field.value = "B"
            await pilot.pause()
            screen._position_types()
            await pilot.pause()
            menu = screen.query_one("#create-type-options", OptionList)
            fields = screen.query_one("#create-table-fields")
            assert menu.display
            assert menu.region.x == field.region.x
            assert menu.region.y == field.region.bottom
            fields.scroll_end(animate=False)
            await pilot.pause()
            assert fields.scroll_y > 0
            assert field.has_focus
            assert field.region.y < fields.content_region.y
            screen._position_types()
            assert not menu.display
            assert field.value == "B"
            assert screen.values()["rows"] == [("id", "B"), ("value", "NUMERIC")]
            assert app.session.executed == []

    asyncio.run(exercise())
