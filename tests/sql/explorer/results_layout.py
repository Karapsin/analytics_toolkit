from __future__ import annotations

import asyncio
from inspect import signature

import pandas as pd
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.results_layout import ResultsSeparator
from textual import events
from textual.widgets import Input

from tests.sql.explorer.app import FakeSession


def test_results_orientation_sizes_bounds_and_keyboard_mode() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 40)) as pilot:
            workspace = app.active_workspace
            app.show_dataframe(pd.DataFrame({"value": [1, 2]}))
            await pilot.pause()
            pane = workspace.query_one(".result-pane")
            editor = workspace.query_one(".query-pane")
            original = pane.size.height
            app._command_results(["expand", "3"])
            await pilot.pause()
            assert pane.size.height == original + 3
            app._command_results(["switch"])
            await pilot.pause()
            assert pane.region.x == editor.region.right + 1
            width = pane.size.width
            app._command_results(["shrink", "5"])
            await pilot.pause()
            assert pane.size.width == width - 5
            app._command_results(["switch"])
            await pilot.pause()
            assert pane.size.height == original + 3
            app._command_results(["expand", "1000"])
            await pilot.pause()
            assert editor.region.height >= 6
            app._command_results(["shrink", "2000"])
            await pilot.pause()
            assert pane.region.height >= 6
            app._command_results(["switch"])
            await pilot.pause()
            assert pane.size.width == width - 5
            app.close_results()
            app._command_results(["switch"])
            assert not workspace.results_open

    asyncio.run(exercise())


def test_keyboard_mode_and_scrollable_specialized_help() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 40)) as pilot:
            app._command_keyboard(["on"])
            await pilot.press("ctrl+t")
            assert app.keyboard_mode
            await pilot.press("f8")
            assert not app.keyboard_mode
            app._command_help(["movement"])
            message = app.active_workspace.result_message
            assert "mvs" in str(message.render())
            await pilot.pause()
            message.focus()
            await pilot.press("pagedown")
            await pilot.pause()
            assert message.scroll_y > 0
            app._command_help(["shortcuts"])
            assert "F6" in str(app.active_workspace.result_message.render())
            app._command_help([])
            assert "help movement" in str(app.active_workspace.result_message.render())

    asyncio.run(exercise())


def test_separator_drag_and_mode_guards() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 40)) as pilot:
            app.show_dataframe(pd.DataFrame({"x": [1]}))
            await pilot.pause()
            workspace = app.active_workspace
            separator = workspace.query_one(ResultsSeparator)

            def mouse(kind, x=5, y=20, button=1):
                arguments = [x, y, 0, 0, button, False, False, False]
                if "widget" in signature(kind).parameters:
                    arguments.insert(0, None)
                return kind(*arguments, screen_x=x, screen_y=y)

            separator.on_mouse_move(mouse(events.MouseMove))
            separator.on_mouse_down(mouse(events.MouseDown, button=2))
            assert separator._drag_origin is None
            for orientation in ("horizontal", "vertical"):
                workspace.results_orientation = orientation
                workspace.apply_results_layout()
                await pilot.pause()
                separator.on_mouse_down(mouse(events.MouseDown))
                original = separator._initial_size
                separator.on_mouse_move(mouse(events.MouseMove, x=3, y=18))
                assert workspace.result_sizes[orientation] == original + 2
                app.keyboard_mode = True
                separator.on_mouse_move(mouse(events.MouseMove, x=1, y=16))
                assert workspace.result_sizes[orientation] == original + 2
                separator.on_mouse_up(mouse(events.MouseUp))
                separator.on_mouse_down(mouse(events.MouseDown))
                assert separator._drag_origin is None
                app.keyboard_mode = False
            assert app.mouse_captured is None

    asyncio.run(exercise())


def test_layout_and_mode_command_validation() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            for arguments in (["expand", "0"], ["bad"], ["shrink"]):
                app._command_results(arguments)
                assert "Error" in str(app.active_workspace.result_message.render())
            for arguments in (["bad"], []):
                app._command_keyboard(arguments)
                assert not app.keyboard_mode
            app.close_results()
            app.active_workspace.result_sizes["vertical"] = None
            app._command_results(["switch"])
            app._command_results(["expand", "2"])
            assert app.active_workspace.result_sizes["vertical"] is not None

    asyncio.run(exercise())


def test_command_history_retains_draft_and_explicit_pane_actions() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            command = app.active_workspace.command_input
            command.remember_command("")
            command.action_history_previous()
            command.action_history_next()
            command.remember_command("mv 1")
            command.remember_command("mv 2")
            command.value = "draft"
            command.action_history_previous()
            command.action_history_previous()
            command.action_history_next()
            assert command.value == "mv 2"
            command.action_history_next()
            assert command.value == "draft"
            app.show_message("hello")
            command.action_focus_previous_pane()
            command.action_focus_next_pane()
            message = app.active_workspace.result_message
            message.action_focus_previous_pane()
            message.action_focus_next_pane()
            message.action_close_results()
            assert not app.active_workspace.results_open

    asyncio.run(exercise())


def test_result_arrows_stay_in_pane_and_initial_layout_is_safe() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        app.active_workspace.apply_results_layout()
        async with app.run_test() as pilot:
            app.show_dataframe(pd.DataFrame({"x": [1, 2, 3]}))
            table = app.active_workspace.result_table
            table.focus()
            await pilot.pause()
            table.move_cursor(row=2, column=0)
            table.action_cursor_up()
            assert table.cursor_row == 1
            assert app.focused is table
            app.on_input_submitted(Input.Submitted(Input(id="command-input"), ""))
            editor = app.active_workspace.editor
            editor.text = "one\ntwo"
            editor.cursor_location = (0, 0)
            editor.add_cursors(20, -1)
            assert editor.cursor_count == 1
            assert editor.text == "one\ntwo"

    asyncio.run(exercise())
