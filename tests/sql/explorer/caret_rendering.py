from __future__ import annotations

import asyncio

from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from textual.document._document import Selection

from tests.sql._support.explorer import FakeSession


def test_first_sql_character_and_all_find_matches_survive_carets_and_selection() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 40)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "SELECT order_id FROM orders WHERE order_date > 0"
            editor._set_selections(Selection.cursor((0, 0)), [Selection.cursor((0, 7))])
            editor.set_search_pattern("order")
            await pilot.pause()
            strip = editor.render_line(0)
            assert "SELECT order_id FROM orders WHERE order_date > 0" in strip.text
            assert editor.search_match_count == 3
            for start, end in editor._search_matches:
                match = strip.crop(editor.gutter_width + start[1], editor.gutter_width + end[1])
                assert match.text == "order"
                assert all(segment.style.bgcolor.name == "bright_yellow" for segment in match)
            editor._set_selections(Selection((0, 7), (0, 12)), [])
            await pilot.pause()
            match = editor.render_line(0).crop(editor.gutter_width + 7, editor.gutter_width + 12)
            assert match.text == "order"
            assert all(segment.style.bgcolor.name == "bright_yellow" for segment in match)

    asyncio.run(exercise())


def test_tabs_wide_characters_and_scrolling_keep_terminal_cells_and_text() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "\t界a" + "x" * 180
            editor._set_selections(Selection.cursor((0, 1)), [Selection.cursor((0, 2))])
            await pilot.pause()
            strip = editor.render_line(0)
            offset, _ = editor.wrapped_document.location_to_offset((0, 1))
            wide = strip.crop(editor.gutter_width + offset, editor.gutter_width + offset + 2)
            assert wide.text == "界"
            assert all(segment.style.underline for segment in wide)
            assert strip.cell_length == editor.scrollable_content_region.width
            editor._set_selections(Selection.cursor((0, 150)), [])
            await pilot.pause()
            assert editor.scroll_x > 0
            offset, _ = editor.wrapped_document.location_to_offset(editor.cursor_location)
            x = editor.gutter_width + offset - int(editor.scroll_x)
            caret = editor.render_line(0).crop(x, x + 1)
            assert caret.text == "x"
            assert all(segment.style.underline for segment in caret)
            assert editor.text == "\t界a" + "x" * 180

    asyncio.run(exercise())


def test_three_line_find_scene_highlights_active_row_in_widget_and_compositor() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(208, 47)) as pilot:
            editor = app.active_workspace.editor
            editor.text = (
                "SELECT order_id, customer_name, revenue\n"
                "FROM analytics.daily_orders\n"
                "WHERE order_date >= CURRENT_DATE - 30"
            )
            app.action_open_find()
            editor.set_search_pattern("order")
            editor._set_selections(Selection.cursor((0, 0)), [Selection.cursor((2, 6))])
            await pilot.pause()
            assert app.focused.id == "find-pattern"
            assert editor.search_match_count == 3
            composed = app.screen._compositor.render_strips()
            viewport = editor.scrollable_content_region
            for start, end in editor._search_matches:
                row, column = start
                left = editor.gutter_width + column
                widget_match = editor.render_line(row).crop(left, left + end[1] - column)
                composed_match = composed[viewport.y + row].crop(
                    viewport.x + left, viewport.x + left + end[1] - column
                )
                for match in (widget_match, composed_match):
                    assert match.text == "order"
                    assert all(
                        segment.style.bgcolor is not None
                        and segment.style.bgcolor.get_truecolor().hex == "#ffff00"
                        for segment in match
                    )
            first = editor.render_line(0).crop(editor.gutter_width, editor.gutter_width + 6)
            assert first.text == "SELECT"
            assert next(iter(first)).style.underline is True
            secondary = editor.render_line(2).crop(editor.gutter_width + 6, editor.gutter_width + 7)
            assert secondary.text == "o"
            assert all(
                segment.style.underline and segment.style.bold is False for segment in secondary
            )

    asyncio.run(exercise())
