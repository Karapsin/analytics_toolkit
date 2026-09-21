from __future__ import annotations

import asyncio
from inspect import signature

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.movement import move_command
from textual import events
from textual.document._document import Selection

from tests.sql.explorer.app import FakeSession


def test_coordinate_commands_and_inclusive_selection() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "abc\ndefgh\nxyz  "
            for command, arguments, expected in [
                ("mv", [], (0, 0)),
                ("mv", ["2", "3"], (1, 2)),
                ("end", [], (2, 5)),
                ("start", [], (0, 0)),
                ("mv", ["E", "S"], (2, 0)),
            ]:
                move_command(editor, command, arguments)
                assert editor.cursor_location == expected
            move_command(editor, "s", ["1", "2", "2", "3"])
            assert editor.selected_text == "bc\ndef"
            move_command(editor, "s", ["S", "S", "E", "E"])
            assert editor.selected_text == editor.text
            move_command(editor, "mv", ["1", "2"])
            move_command(editor, "mvs", ["2", "3"])
            assert editor.selected_text == "bc\ndef"
            move_command(editor, "mvs", ["S"])
            assert editor.selected_text == "def"
            before = editor.selection
            for command, args in [
                ("mv", ["0"]),
                ("mv", ["1", "1", "1"]),
                ("mvs", ["2"]),
                ("cursor", ["0", "u"]),
                ("d", ["-1"]),
                ("d", ["1", "2"]),
                ("s", []),
                ("mv", ["s", "2"]),
                ("cursor", ["2", "U"]),
                ("start", ["1"]),
                ("mv", ["1", "0"]),
            ]:
                with pytest.raises(ValueError, match=r"Row|Usage|Count|Column"):
                    move_command(editor, command, args)
                assert editor.selection == before

    asyncio.run(exercise())


def test_absolute_move_extends_document_with_bounded_undoable_newlines() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "first\nsecond"
            source = editor.text
            move_command(editor, "mv", ["10"])
            assert editor.text == source + "\n" * 8
            assert editor.cursor_location == (9, 0)
            editor.action_undo()
            assert editor.text == source
            move_command(editor, "mv", ["1000", "E"])
            assert editor.document.line_count == 102
            assert editor.cursor_location == (101, 0)
            before = editor.text
            for command, arguments in [
                ("mv", ["200", "bad"]),
                ("mvs", ["200", "S"]),
                ("s", ["S", "S", "200", "E"]),
            ]:
                with pytest.raises(ValueError, match=r"Column|Row|invalid literal"):
                    move_command(editor, command, arguments)
                assert editor.text == before
            for command in ("d", "u", "pd", "pu"):
                move_command(editor, command, ["1000"])
                assert editor.text == before

    asyncio.run(exercise())


def test_navigation_restores_column_and_never_changes_document() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "abcdefgh\nx\nabcdefgh\nabcdefgh"
            editor.cursor_location = (0, 6)
            move_command(editor, "d", [])
            assert editor.cursor_location == (1, 1)
            move_command(editor, "d", [])
            assert editor.cursor_location == (2, 6)
            move_command(editor, "cursor", ["2", "u"])
            assert editor.selection == Selection.cursor((2, 6))
            editor.action_cursor_left()
            assert [selection.end for selection in editor.cursor_selections] == [
                (0, 5),
                (1, 1),
                (2, 5),
            ]
            editor.action_cursor_right()
            assert editor.logical_column == 6
            source = editor.text
            for command in ("d", "u", "pd", "pu"):
                move_command(editor, command, ["100"])
                assert editor.text == source
            assert editor.cursor_location[0] == 0
            editor._set_selections(Selection.cursor((0, 0)), [])
            editor.action_cursor_left()
            assert editor.cursor_location == (0, 0)

    asyncio.run(exercise())


def test_tab_pairs_history_and_keyboard_mode() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            editor = app.active_workspace.editor
            editor.text = "  SELECT "
            editor.cursor_location = (0, 9)
            editor.focus()
            await pilot.press("tab")
            assert editor.text == "  SELECT * "
            assert editor.cursor_location == (0, 11)
            await pilot.press("tab")
            assert editor.text == "  SELECT *\n  from "
            editor.text = ""
            editor.cursor_location = (0, 0)
            await pilot.press("(")
            assert editor.text == "()"
            assert editor.cursor_location == (0, 1)
            await pilot.press("backspace")
            assert editor.text == ""
            await pilot.press("[", "]")
            assert editor.text == "[]"
            assert editor.cursor_location == (0, 2)
            editor.text = "hello"
            editor.selection = Selection((0, 0), (0, 5))
            await pilot.press("(")
            assert editor.text == "(hello)"
            command = app.active_workspace.command_input
            command.remember_command("mv 1")
            command.value = "draft"
            command.cursor_position = 2
            command.focus_preserving_cursor()
            await pilot.press("up")
            assert command.value == "mv 1"
            assert app.focused is command
            await pilot.press("down")
            assert command.value == "draft"
            assert command.cursor_position == 2
            app._command_keyboard(["on"])
            editor.focus()
            editor.cursor_location = (0, 7)
            await pilot.press("x")
            assert editor.text == "(hello)x"
            assert not editor.read_only
            location = editor.cursor_location
            await pilot.click("#query-editor", offset=(1, 0))
            assert editor.cursor_location == location
            app._command_keyboard(["off"])

    asyncio.run(exercise())


def test_word_line_and_page_actions_and_command_dispatch() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "one two three\nlast line"
            app._handle_command(["d"])
            assert editor.cursor_location == (1, 0)
            editor.move_to_line_start(1)
            move_command(editor, "mv", ["n", "2"])
            assert editor.cursor_location == (0, 7)
            move_command(editor, "mvs", ["p"])
            assert editor.selected_text == "two"
            editor.select_to_line_start(1)
            assert editor.selected_text == "one "
            editor.action_cursor_page_down()
            assert editor.cursor_location[0] == 1
            editor.action_cursor_page_up()
            assert editor.cursor_location[0] == 0
            app._command_navigation("mv", ["0"])
            assert "Row" in str(app.active_workspace.result_message.render())

    asyncio.run(exercise())


def test_keyboard_mode_filters_mouse_events_and_keeps_other_controls() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            editor = app.active_workspace.editor
            app.keyboard_mode = True

            def mouse(x, y):
                arguments = [x, y, 0, 0, 1, False, False, False]
                if "widget" in signature(events.MouseDown).parameters:
                    arguments.insert(0, None)
                return events.MouseDown(*arguments)

            event = mouse(editor.region.x + 2, editor.region.y + 1)
            await app.on_event(event)
            assert event._stop_propagation
            await app.on_event(mouse(-1, -1))
            await app.on_event(
                mouse(
                    app.active_workspace.command_input.region.x,
                    app.active_workspace.command_input.region.y,
                )
            )
            app.action_open_find()
            editor.focus()
            app.active_workspace.find_bar.focus_relative(1)
            app.action_find_previous_control()
            await pilot.pause()

    asyncio.run(exercise())
