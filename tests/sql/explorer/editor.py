from __future__ import annotations

import asyncio
from dataclasses import replace
from inspect import signature
from typing import TYPE_CHECKING, Any

from analytics_toolkit.sql_explorer.app import (
    ConfirmMutationScreen,
    ResultMessage,
    SqlEditor,
    SqlExplorerApp,
)
from textual import events
from textual._tree_sitter import TREE_SITTER
from textual.document._document import Selection
from textual.widgets import Static, TextArea

from tests.sql.explorer.app import FakeSession

if TYPE_CHECKING:
    import pytest


def _click_event() -> events.Click:
    arguments: list[Any] = [0, 0, 0, 0, 1, False, False, False]
    if "widget" in signature(events.Click).parameters:
        arguments.insert(0, None)
    return events.Click(*arguments)


def test_selected_sql_executes_exactly_and_preserves_selection() -> None:
    async def exercise() -> None:
        session = FakeSession()
        application = SqlExplorerApp(session)
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "select 0\nselect 1\nselect 2"
            editor.selection = Selection((1, 0), (1, len("select 1")))
            expected = editor.selection

            await pilot.press("f5")
            await pilot.pause()

            assert session.executed[0].statements == ("select 1",)
            assert "select 0" not in session.executed[0].execution_sql
            assert editor.selection == expected
            assert editor.text == "select 0\nselect 1\nselect 2"

    asyncio.run(exercise())


def test_selected_multi_statement_routing_and_confirmation_preview() -> None:
    async def exercise() -> None:
        session = FakeSession()
        application = SqlExplorerApp(session)
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "select 0;\ndelete from chosen;\nselect 2;"
            editor.selection = Selection((1, 0), (1, len("delete from chosen;")))

            await pilot.press("f5")

            assert isinstance(application.screen, ConfirmMutationScreen)
            preview = application.screen.query_one("#confirmation-preview", Static)
            rendered = str(preview.render())
            assert "delete from chosen" in rendered
            assert "select 0" not in rendered
            assert "select 2" not in rendered

            await pilot.press("escape")
            await pilot.pause()
            session.settings = replace(session.settings, confirm_mutations=False)
            editor.selection = Selection((0, 0), (1, len("delete from chosen;")))
            await pilot.press("f5")
            await pilot.pause()
            assert session.executed[0].statement_count == 2

    asyncio.run(exercise())


def test_whitespace_only_selection_is_not_replaced_by_full_buffer() -> None:
    async def exercise() -> None:
        session = FakeSession()
        application = SqlExplorerApp(session)
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "select 1\n   \nselect 2"
            editor.selection = Selection((1, 0), (1, 3))

            await pilot.press("f5")

            assert session.executed == []
            error = str(application.query_one(ResultMessage).render()).lower()
            assert "enter a sql statement" in error

    asyncio.run(exercise())


def test_home_uses_selection_endpoint_row_and_always_collapses() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "zero\none line\ntwo line"
            editor.selection = Selection((0, 2), (2, 4))

            await pilot.press("home")
            assert editor.selection == Selection.cursor((2, 0))

            editor.selection = Selection((2, 3), (0, 2))
            await pilot.press("home")
            assert editor.selection == Selection.cursor((0, 0))

            editor.cursor_location = (2, 4)
            await pilot.press("ctrl+home")
            assert editor.selection == Selection.cursor((0, 0))

    asyncio.run(exercise())


def test_shift_vertical_keys_chain_and_toggle_multi_cursors() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "first\nsecond\nthird"
            editor.cursor_location = (2, 3)

            await pilot.press("shift+up")
            assert editor.selection == Selection.cursor((1, 3))
            assert editor.cursor_count == 2
            await pilot.press("shift+up")
            assert editor.selection == Selection.cursor((0, 3))
            assert editor.cursor_count == 3
            await pilot.press("shift+down")
            assert editor.cursor_count == 2
            assert editor.selection == Selection.cursor((0, 3))

    asyncio.run(exercise())


def test_secondary_cursor_is_visible_on_an_empty_line() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "first\n\nthird"
            editor.cursor_location = (2, 0)

            await pilot.press("shift+up", "shift+up")

            rendered = editor.get_line(1)
            assert rendered.plain == " "
            assert any(span.start == 0 and span.end == 1 for span in rendered.spans)

    asyncio.run(exercise())


def test_selected_indent_and_unindent_include_inserted_spaces() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            editor = application.query_one(SqlEditor)
            editor.text = "one\ntwo\nthree"
            editor.selection = Selection((0, 1), (1, 3))

            editor.action_indent()
            assert editor.text == "    one\n    two\nthree"
            assert editor.selection == Selection((0, 0), (1, 7))
            assert editor.selected_text == "    one\n    two"

            editor.action_unindent()
            assert editor.text == "one\ntwo\nthree"
            assert editor.selection == Selection((0, 0), (1, 3))

            editor.move_cursor((0, 3))
            editor.action_indent()
            assert editor.text.startswith("one    ")

    asyncio.run(exercise())


def test_double_click_tracking_selects_identifier_with_underscore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            editor = application.query_one(SqlEditor)
            editor.text = "select table_name from sample"
            monkeypatch.setattr(editor, "get_target_document_location", lambda _event: (0, 10))
            click = _click_event()
            await editor._on_click(click)
            await editor._on_click(_click_event())
            assert editor.selected_text == "table_name"

            editor.text = ""
            editor.action_double_click_word((0, 0))
            assert editor.selection == Selection.cursor((0, 0))

            editor.text = "!"
            editor.action_double_click_word((0, 0))
            assert editor.selection == Selection.cursor((0, 0))

            editor.text = "L"
            editor.cursor_location = (0, 1)
            await editor._on_key(events.Key("tab", "\t"))
            assert application.query_one("#completion-menu").styles.display == "block"
            await editor._on_key(events.Key("x", "x"))
            assert editor.text == "Lx"

    asyncio.run(exercise())


def test_sql_editor_configuration_and_parser_failure_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            editor = application.query_one(SqlEditor)
            assert editor.language == "sql"
            expected_document = "SyntaxAwareDocument" if TREE_SITTER else "Document"
            assert type(editor.document).__name__ == expected_document
            assert editor.show_line_numbers is True
            assert editor.soft_wrap is False
            assert editor.tab_behavior == "indent"
            assert editor.cursor_blink is False
            assert editor._cursor_visible is True
            await asyncio.sleep(0.6)
            assert editor._cursor_visible is True

            original = TextArea.__init__
            calls: list[dict[str, Any]] = []

            def fail_first(self: TextArea, *args: Any, **kwargs: Any) -> None:
                calls.append(kwargs.copy())
                if len(calls) == 1:
                    message = "parser unavailable"
                    raise ValueError(message)
                original(self, *args, **kwargs)

            monkeypatch.setattr(TextArea, "__init__", fail_first)
            fallback = SqlEditor()
            assert fallback.language is None
            assert fallback.cursor_blink is False
            assert calls[0]["language"] == "sql"
            assert calls[1]["language"] is None

    asyncio.run(exercise())


def test_bracketed_paste_replaces_editor_selection() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            editor = application.query_one(SqlEditor)
            editor.text = "select old"
            editor.selection = Selection((0, 7), (0, 10))
            await editor._on_paste(events.Paste("new\nvalue"))
            assert editor.text == "select new\nvalue"

    asyncio.run(exercise())
