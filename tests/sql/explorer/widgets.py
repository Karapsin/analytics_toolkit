from __future__ import annotations

import asyncio
from types import SimpleNamespace

from analytics_toolkit.sql_explorer.app import FindReplaceBar, SqlEditor, SqlExplorerApp
from textual.widgets import Input, Static

from tests.sql.explorer.app import FakeSession


def test_find_replace_ui_highlights_navigates_and_replaces() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(100, 35)) as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "alpha alpha\nALPHA"

            await pilot.press("ctrl+f")
            bar = application.query_one(FindReplaceBar)
            search_input = application.query_one("#find-pattern", Input)
            replacement_input = application.query_one("#replace-pattern", Input)
            assert bar.styles.display != "none"
            assert application.focused is search_input

            search_input.value = "alpha"
            await pilot.pause()
            assert editor.search_match_count == 3
            highlighted_line = editor.get_line(0)
            assert sum(span.style.bgcolor is not None for span in highlighted_line.spans) == 2

            await pilot.press("enter")
            assert editor.selected_text.lower() == "alpha"
            assert editor.search_position == 1

            await pilot.click("#find-next")
            assert editor.search_position == 2

            replacement_input.value = "beta"
            await pilot.click("#replace-current")
            assert editor.text == "alpha beta\nALPHA"
            assert editor.selected_text.lower() == "alpha"

            await pilot.click("#replace-all")
            assert editor.text == "beta beta\nbeta"
            assert "Replaced 2 occurrence(s)" in str(
                application.query_one("#notice", Static).render()
            )

            await pilot.press("escape")
            assert bar.styles.display == "none"
            assert application.focused is editor
            assert editor.search_match_count == 0

    asyncio.run(exercise())


def test_find_prefills_single_line_selection_and_reports_empty_search() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "find this\nand this"
            editor.move_cursor((0, 0))
            editor.move_cursor((0, 4), select=True)

            await pilot.press("ctrl+f")
            search_input = application.query_one("#find-pattern", Input)
            assert search_input.value == "find"

            search_input.value = "missing"
            await pilot.pause()
            assert "No matches" in str(application.query_one("#notice", Static).render())

            search_input.value = ""
            await pilot.press("enter")
            assert "Enter text" in str(application.query_one("#notice", Static).render())

            bar = application.query_one(FindReplaceBar)
            replacement_input = application.query_one("#replace-pattern", Input)
            bar.on_input_submitted(SimpleNamespace(input=replacement_input))
            bar.on_input_submitted(SimpleNamespace(input=SimpleNamespace(id="other")))
            bar.on_button_pressed(SimpleNamespace(button=SimpleNamespace(id="other")))

            assert editor.replace_current_search_match("replacement") is False
            assert editor.replace_all_search_matches("replacement") == 0
            editor.set_search_pattern("missing")
            assert editor.replace_all_search_matches("replacement") == 0

            editor.text = "only"
            editor.set_search_pattern("only")
            assert editor.replace_current_search_match("gone") is True
            assert editor.search_match_count == 0

    asyncio.run(exercise())
