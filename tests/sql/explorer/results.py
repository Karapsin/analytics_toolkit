from __future__ import annotations

import asyncio
from inspect import signature
from typing import Any

import pandas as pd
from analytics_toolkit.sql_explorer.app import ResultTable, SqlExplorerApp
from rich.style import Style
from textual import events
from textual.coordinate import Coordinate

from tests.sql.explorer.app import FakeSession


def _mouse_event(
    event_type: type[events.MouseEvent],
    row: int,
    column: int,
    *,
    shift: bool = False,
) -> events.MouseEvent:
    arguments: list[Any] = [0, 0, 0, 0, 1, shift, False, False]
    if "widget" in signature(event_type).parameters:
        arguments.insert(0, None)
    return event_type(*arguments, style=Style.from_meta({"row": row, "column": column}))


def _plain_mouse_event(event_type: type[events.MouseEvent]) -> events.MouseEvent:
    arguments: list[Any] = [0, 0, 0, 0, 1, False, False, False]
    if "widget" in signature(event_type).parameters:
        arguments.insert(0, None)
    return event_type(*arguments)


def test_result_rows_are_numbered_from_one_but_copy_uses_only_cells() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            application.show_dataframe(pd.DataFrame({"a": ["x", "y"], "b": [1, 2]}))
            table = application.query_one(ResultTable)

            assert str(table.ordered_rows[0].label) == "1"
            assert str(table.ordered_rows[1].label) == "2"
            table.set_cell_selection(0, 0)
            table.set_cell_selection(1, 1, extend=True)
            assert table.copy_text() == "x\t1\ny\t2"
            assert "1\tx" not in table.copy_text()

    asyncio.run(exercise())


def test_keyboard_rectangular_selection_and_plain_movement() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            application.show_dataframe(
                pd.DataFrame({"a": ["a0", "a1", "a2"], "b": ["b0", "b1", "b2"]})
            )
            table = application.query_one(ResultTable)
            table.focus()
            table.set_cell_selection(0, 0)

            await pilot.press("shift+right", "shift+down")
            assert table.selected_cells == ((0, 0), (1, 1))
            assert table.copy_text() == "a0\tb0\na1\tb1"
            assert table._is_selected(1, 1) is True
            assert table._is_selected(2, 1) is False

            await pilot.press("down")
            assert table.cursor_coordinate == Coordinate(2, 1)
            assert table.selected_cells is None

    asyncio.run(exercise())


def test_mouse_drag_shift_click_and_header_only_selection() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            application.show_dataframe(
                pd.DataFrame({"first": ["a0", "a1"], "second": ["b0", "b1"]})
            )
            table = application.query_one(ResultTable)

            await table._on_mouse_down(_mouse_event(events.MouseDown, 0, 0))
            table._on_mouse_move(_mouse_event(events.MouseMove, 1, 1))
            await table._on_mouse_up(_mouse_event(events.MouseUp, 1, 1))
            assert table.selected_cells == ((0, 0), (1, 1))
            # Textual emits a Click after MouseUp; it must not collapse the drag.
            await table._on_click(_mouse_event(events.Click, 1, 1))

            await table._on_click(_mouse_event(events.Click, 0, 1, shift=True))
            assert table.selected_cells == ((0, 0), (0, 1))

            await table._on_click(_mouse_event(events.Click, -1, 1))
            assert table.selected_header == "second"
            assert table.copy_text() == "second"
            assert table.selected_cells is None
            assert table._is_selected(-1, 1) is True

    asyncio.run(exercise())


def test_visible_cell_formatting_cannot_break_tsv_boundaries() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            application.show_dataframe(pd.DataFrame({"text": ["a\tb\nc"], "missing": [None]}))
            table = application.query_one(ResultTable)
            table.set_cell_selection(0, 0)
            table.set_cell_selection(0, 1, extend=True)

            assert str(table.get_cell_at(Coordinate(0, 0))) == "a\\tb\\nc"
            assert table.copy_text() == "a\\tb\\nc\tNULL"

    asyncio.run(exercise())


def test_result_selection_defensive_mouse_and_arrow_paths() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            table = application.query_one(ResultTable)
            table._extend_by(1, 1)
            application.show_dataframe(
                pd.DataFrame({"first": ["a0", "a1"], "second": ["b0", "b1"]})
            )
            table.set_cell_selection(1, 1)
            table.action_cursor_up(select=True)
            table.action_cursor_left(select=True)
            assert table.selected_cells == ((1, 1), (0, 0))

            table.action_cursor_left()
            table.action_cursor_right()
            table.clear_rectangular_selection()
            assert table.copy_text() == str(table.get_cell_at(table.cursor_coordinate))

            plain_down = _plain_mouse_event(events.MouseDown)
            await table._on_mouse_down(plain_down)
            await table._on_mouse_down(_mouse_event(events.MouseDown, -2, -1))

            table._dragging = False
            table._on_mouse_move(_plain_mouse_event(events.MouseMove))
            table._dragging = True
            table._on_mouse_move(_plain_mouse_event(events.MouseMove))
            table._dragging = False
            await table._on_mouse_up(_plain_mouse_event(events.MouseUp))

            await table._on_click(_plain_mouse_event(events.Click))
            await table._on_click(_mouse_event(events.Click, -2, -1))
            assert table._event_coordinate(plain_down) is None

    asyncio.run(exercise())
