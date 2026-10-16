from __future__ import annotations

import asyncio

from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.create_table_screen import CreateTableScreen
from textual.widgets import Collapsible, Input, OptionList

from tests.sql._support.explorer import FakeSession
from tests.sql._support.type_menu_diagnostics import menu_snapshot


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
            initial = menu_snapshot(app, screen, field, menu, fields)
            assert menu.display, initial
            assert menu.region.x == field.region.x, initial
            assert menu.region.y == field.region.bottom, initial
            fields.scroll_end(animate=False)
            await pilot.pause()
            scrolled = menu_snapshot(app, screen, field, menu, fields)
            assert fields.scroll_y > 0, scrolled
            assert field.has_focus, scrolled
            assert field.region.y < fields.content_region.y, scrolled
            screen._position_types()
            positioned = menu_snapshot(app, screen, field, menu, fields)
            assert not menu.display, positioned
            assert field.value == "B", positioned
            assert screen.values()["rows"] == [("id", "B"), ("value", "NUMERIC")], positioned
            assert app.session.executed == [], positioned

    asyncio.run(exercise())
