from __future__ import annotations

import asyncio

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.editor_actions import PAIRS, code_context, completion_text
from textual.document._document import Selection

from tests.sql.explorer.app import FakeSession


@pytest.mark.parametrize(("opener", "closer"), list(PAIRS.items()))
def test_pair_insertion_wrapping_skipping_and_backspace(opener: str, closer: str) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            editor = app.active_workspace.editor
            editor.text = "\n"
            editor.add_cursors(1, 1)
            await pilot.press(opener)
            assert editor.text == opener + closer + "\n" + opener + closer
            assert [item.end for item in editor.cursor_selections] == [(0, 1), (1, 1)]
            await pilot.press("backspace")
            assert editor.text == "\n"
            await pilot.press(opener, closer)
            assert editor.text == opener + closer + "\n" + opener + closer
            editor._set_selections(Selection((0, 0), (0, 2)), [])
            await pilot.press(opener)
            assert editor.text.splitlines()[0] == opener + opener + closer + closer
            editor.action_undo()
            assert editor.text.splitlines()[0] == opener + closer

    asyncio.run(exercise())


@pytest.mark.parametrize("suffix", [",", ")", ";", ".", " ", "\n"])
def test_completion_does_not_add_space_before_punctuation_or_whitespace(suffix: str) -> None:
    assert completion_text("contact_id", suffix) == "contact_id"
    assert completion_text("contact_id", "") == "contact_id "
    assert completion_text("schema.", "") == "schema."


@pytest.mark.parametrize(
    "source",
    [
        "/* unfinished\nselect ",
        "-- select ",
        "select 'unfinished",
        'select "unfinished',
        "select `unfinished",
    ],
)
def test_unfinished_comments_and_literals_disable_pairing(source: str) -> None:
    assert not code_context(source, len(source))
    assert code_context("select ", len("select "))
