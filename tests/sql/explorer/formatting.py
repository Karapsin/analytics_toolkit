from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit import sql_format
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.formatting import format_editor, format_script
from textual.document._document import Selection

from tests.sql.explorer.app import FakeSession

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("backend", "dialect"), [("gp", "postgres"), ("trino", "trino"), ("ch", "clickhouse")]
)
def test_script_uses_repository_formatter_and_preserves_comments_and_terminators(
    backend: str,
    dialect: str,
) -> None:
    first = "select 'a;b' as value;"
    second = "select id from orders where id > 1"
    assert format_script(f"{first}\n{second}", backend) == (
        sql_format.format_sql(first, dialect=dialect)
        + "\n\n"
        + sql_format.format_sql(second, dialect=dialect)
    )
    formatted = format_script("-- first query\nselect 1;\n-- trailing comment", backend)
    assert "first query" in formatted
    assert formatted.endswith("-- trailing comment")
    assert format_script("  \n", backend) == "  \n"
    assert format_script("-- only a comment", backend) == "-- only a comment"
    inline = format_script("select 1; -- keep here\nselect 2;", backend)
    assert "; -- keep here\n\n" in inline
    assert inline.endswith(";")


def test_format_command_changes_only_active_buffer_with_one_undo(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            first = application.active_workspace
            first.editor.text = "select 0"
            await pilot.press("ctrl+t")
            await pilot.pause()
            workspace = application.active_workspace
            original = "select id from orders where id > 1;\nselect 2;"
            workspace.editor.text = original
            workspace.current_file = tmp_path / "query.sql"
            workspace.saved_text = original
            workspace.command_input.focus()
            workspace.command_input.value = ":format"
            await pilot.press("enter")
            await pilot.pause()
            assert workspace.editor.text == format_script(original, "gp")
            assert workspace.is_dirty
            assert application.focused is workspace.command_input
            assert first.editor.text == "select 0"
            workspace.editor.action_undo()
            await pilot.pause()
            assert workspace.editor.text == original
            assert not workspace.is_dirty
            assert not workspace.session.executed

    asyncio.run(exercise())


def test_multiple_selections_are_atomic_and_keep_unselected_text() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.active_workspace.editor
            original = "select a from t\nuntouched\nselect b from u"
            editor.text = original
            editor._set_selections(Selection((2, 0), (2, 15)), [Selection((0, 0), (0, 15))])
            assert format_editor(editor, "gp")
            expected_first = format_script("select a from t", "gp")
            expected_second = format_script("select b from u", "gp")
            assert editor.text == f"{expected_first}\nuntouched\n{expected_second}"
            assert all(selection.is_empty for selection in editor.cursor_selections)
            assert editor.selection.end == editor.document.end
            editor.action_undo()
            await pilot.pause()
            assert editor.text == original

            editor.text = "select a from t;\nselect ("
            editor._set_selections(Selection((1, 0), (1, 8)), [Selection((0, 0), (0, 16))])
            before = editor.text
            selections = editor.cursor_selections
            application._command_format([])
            assert editor.text == before
            assert editor.cursor_selections == selections
            assert application.active_workspace.results_open

    asyncio.run(exercise())


def test_overlap_noop_arguments_and_formatting_while_busy() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.active_workspace.editor
            original = "select a,\nb from t;"
            editor.text = original
            editor._set_selections(Selection((0, 0), (1, 9)), [Selection((0, 0), (0, 9))])
            workspace = application.active_workspace
            workspace.busy = True
            application._command_format([])
            assert editor.text == format_script(original, "gp")
            assert workspace.busy
            workspace.busy = False
            await pilot.pause()
            selections = editor.cursor_selections
            assert not format_editor(editor, "gp")
            assert editor.cursor_selections == selections
            before = editor.text
            application._command_format(["unexpected"])
            assert editor.text == before
            assert "Usage: format" in str(workspace.result_message.render())
            editor.text = " \n "
            assert not format_editor(editor, "gp")

    asyncio.run(exercise())


def test_formatting_moves_unselected_cursors_with_surrounding_text() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            editor = application.active_workspace.editor
            editor.text = "header\nselect a from t\nfooter"
            editor._set_selections(
                Selection.cursor((2, 3)),
                [Selection.cursor((0, 2)), Selection((1, 0), (1, 15))],
            )
            assert format_editor(editor, "gp")
            assert editor.text.startswith("header\n")
            assert editor.text.endswith("\nfooter")
            assert editor.selection.end == (editor.document.line_count - 1, 3)
            assert Selection.cursor((0, 2)) in editor.cursor_selections

    asyncio.run(exercise())
