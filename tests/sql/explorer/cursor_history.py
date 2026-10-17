from __future__ import annotations

import asyncio

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.editor import SqlEditor
from textual.document._document import Selection

from tests.sql._support.explorer import FakeSession


def test_repeated_indent_after_undo_releases_discarded_redo_edits() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            original = "\n".join(["x" * 256] * 8)
            editor.text = original
            editor._set_selections(Selection((7, 256), (0, 0)), [])
            abandoned = None
            for _ in range(12):
                editor.action_indent()
                assert editor.text == "\n".join(["    " + "x" * 256] * 8)
                assert len(editor._cursor_edits) == 1
                recorded = editor._cursor_edits[0][0]
                assert recorded is editor.history.undo_stack[-1][-1]
                assert recorded is not abandoned
                assert not editor.history.redo_stack
                editor.action_undo()
                assert editor.text == original
                assert editor.selection == Selection((7, 256), (0, 0))
                assert len(editor._cursor_edits) == 1
                assert editor._cursor_edits[0][0] is editor.history.redo_stack[-1][-1]
                abandoned = recorded

    asyncio.run(exercise())


def test_indent_unindent_and_cursor_edits_restore_direction_and_independent_columns() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "one\ntwo\nthree"
            active = Selection((2, 5), (2, 0))
            secondary = Selection((0, 0), (1, 3))
            editor._set_selections(active, [secondary], preferences={active: 11, secondary: 7})

            def state() -> tuple:
                return (
                    editor.text,
                    editor.selection,
                    editor.cursor_selections,
                    dict(editor._cursor_preferences),
                )

            states = [state()]
            editor.action_indent()
            assert editor.text == "    one\n    two\n    three"
            assert editor.selection == Selection((2, 9), (2, 4))
            assert editor.logical_column == 15
            states.append(state())
            editor.action_unindent()
            assert state() == states[0]
            states.append(state())
            editor._apply_batch_edit("!")
            assert editor.text == "!\n!"
            assert editor.selection == Selection.cursor((1, 1))
            assert editor.cursor_count == 2
            states.append(state())
            for expected in reversed(states[:-1]):
                editor.action_undo()
                assert state() == expected
            for expected in states[1:]:
                editor.action_redo()
                assert state() == expected
            assert len(editor._cursor_edits) == 3
            live = {
                id(edit)
                for batch in (*editor.history.undo_stack, *editor.history.redo_stack)
                for edit in batch
            }
            recorded = [id(entry[0]) for entry in editor._cursor_edits]
            assert len(set(recorded)) == len(recorded)
            assert set(recorded) <= live

    asyncio.run(exercise())


@pytest.mark.parametrize("replacement", ["unindent", "edit"])
def test_other_transactions_release_an_abandoned_indent_branch(replacement: str) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "    one\n    two"
            original = Selection((1, 7), (0, 0))
            editor._set_selections(original, [], preferences={original: 10})
            editor.action_indent()
            abandoned = editor._cursor_edits[0][0]
            editor.action_undo()
            if replacement == "unindent":
                editor.action_unindent()
                assert editor.text == "one\ntwo"
            else:
                editor._apply_batch_edit("!")
                assert editor.text == "!"
            assert len(editor._cursor_edits) == 1
            assert editor._cursor_edits[0][0] is not abandoned
            assert editor._cursor_edits[0][0] is editor.history.undo_stack[-1][-1]
            assert not editor.history.redo_stack
            editor.action_undo()
            assert editor.text == "    one\n    two"
            assert editor.selection == original
            assert editor.logical_column == 10

    asyncio.run(exercise())


def test_history_eviction_keeps_only_live_cursor_snapshots_and_undoable_edits() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "one\ntwo"
            editor.history = type(editor.history)(
                max_checkpoints=2, checkpoint_timer=60.0, checkpoint_max_characters=1000
            )
            editor._set_selections(Selection((0, 0), (1, 3)), [])
            states = [(editor.text, editor.selection, dict(editor._cursor_preferences))]
            evicted = None
            for count in range(1, 5):
                editor.action_indent()
                assert editor.text == "\n".join("    " * count + word for word in ("one", "two"))
                states.append((editor.text, editor.selection, dict(editor._cursor_preferences)))
                assert len(editor._cursor_edits) == min(count, 2)
                live = {
                    id(edit)
                    for batch in (*editor.history.undo_stack, *editor.history.redo_stack)
                    for edit in batch
                }
                assert {id(entry[0]) for entry in editor._cursor_edits} <= live
                if count == 1:
                    evicted = editor._cursor_edits[0][0]
            assert all(entry[0] is not evicted for entry in editor._cursor_edits)
            for expected in reversed(states[2:4]):
                editor.action_undo()
                assert (editor.text, editor.selection, editor._cursor_preferences) == expected
            editor.action_undo()
            assert (editor.text, editor.selection, editor._cursor_preferences) == states[2]
            for expected in states[3:]:
                editor.action_redo()
                assert (editor.text, editor.selection, editor._cursor_preferences) == expected

    asyncio.run(exercise())


@pytest.mark.parametrize("reset", ["load_text", "text"])
@pytest.mark.parametrize("replacement", ["x", ""])
def test_document_reset_immediately_releases_undo_and_redo_snapshots(
    reset: str, replacement: str
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            editor = app.active_workspace.editor
            editor.text = "one\ntwo\nthree"
            active = Selection((2, 5), (1, 1))
            secondary = Selection((0, 3), (0, 0))
            editor._set_selections(active, [secondary], preferences={active: 11, secondary: 7})
            editor.action_indent()
            editor.action_indent()
            editor.action_undo()
            assert editor.history.undo_stack
            assert editor.history.redo_stack
            assert len(editor._cursor_edits) == 2
            assert editor.cursor_count == 2
            assert set(editor._cursor_preferences.values()) == {11, 15}
            if reset == "load_text":
                editor.load_text(replacement)
            else:
                editor.text = replacement
            assert editor.text == replacement
            assert editor.cursor_count == 1
            assert editor.cursor_selections == (Selection.cursor((0, 0)),)
            assert editor._secondary_selections == []
            assert editor._cursor_preferences == {}
            assert editor.logical_column == 0
            assert editor._cursor_edits == []
            assert not editor.history.undo_stack
            assert not editor.history.redo_stack
            editor.action_undo()
            editor.action_redo()
            assert editor.text == replacement
            assert editor.cursor_selections == (Selection.cursor((0, 0)),)
            editor.focus()
            await pilot.pause()
            for row in range(editor.content_size.height):
                strip = editor.render_line(row)
                assert strip.cell_length == editor.scrollable_content_region.width
            assert editor.text == replacement
            await pilot.press("z")
            assert editor.text == "z" + replacement
            assert editor.cursor_selections == (Selection.cursor((0, 1)),)
            editor.action_undo()
            assert editor.text == replacement
            assert editor.cursor_selections == (Selection.cursor((0, 0)),)
            editor.action_redo()
            assert editor.text == "z" + replacement
            assert editor.cursor_selections == (Selection.cursor((0, 1)),)

    asyncio.run(exercise())


@pytest.mark.parametrize("reset", ["load_text", "text"])
@pytest.mark.parametrize("replacement", ["x", ""])
def test_unmounted_document_reset_clears_secondary_cursors_and_preferences(
    reset: str, replacement: str
) -> None:
    fresh = SqlEditor("initial", soft_wrap=False)
    assert fresh.text == "initial"
    assert fresh._cursor_edits == []
    fresh.action_undo()
    fresh.action_redo()
    assert fresh.text == "initial"
    editor = SqlEditor("one\ntwo\nthree", soft_wrap=False)
    secondary = Selection((2, 5), (1, 1))
    editor._secondary_selections = [secondary]
    editor._cursor_preferences = {editor.selection: 9, secondary: 12}
    if reset == "load_text":
        editor.load_text(replacement)
    else:
        editor.text = replacement
    assert editor.text == replacement
    assert editor.cursor_selections == (Selection.cursor((0, 0)),)
    assert editor._secondary_selections == []
    assert editor._cursor_preferences == {}
    assert editor._cursor_edits == []
    assert not editor.history.undo_stack
    assert not editor.history.redo_stack


@pytest.mark.parametrize("maintenance", ["idle", "undo", "redo", "edit", "unindent"])
def test_explicit_history_clear_is_pruned_by_maintenance_and_noop_transactions(
    maintenance: str,
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "one\ntwo"
            editor._set_selections(Selection((0, 0), (1, 3)), [])
            editor.action_indent()
            editor.action_undo()
            editor._set_selections(Selection.cursor((0, 0)), [])
            assert editor._cursor_edits
            editor.history.clear()
            if maintenance == "idle":
                editor.on_idle()
            elif maintenance == "undo":
                editor.action_undo()
            elif maintenance == "redo":
                editor.action_redo()
            elif maintenance == "edit":
                editor._apply_batch_edit("")
            else:
                editor.action_unindent()
            assert editor._cursor_edits == []
            assert editor.text == "one\ntwo"
            assert not editor.history.undo_stack
            assert not editor.history.redo_stack

    asyncio.run(exercise())


@pytest.mark.parametrize("noop", ["edit", "unindent"])
def test_noop_transaction_preserves_live_redo_snapshots(noop: str) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "one\ntwo"
            original = Selection((1, 3), (0, 0))
            editor._set_selections(original, [], preferences={original: 9})
            editor.action_indent()
            after = editor.text, editor.selection, dict(editor._cursor_preferences)
            editor.action_undo()
            editor._set_selections(Selection.cursor((0, 0)), [])
            recorded = list(editor._cursor_edits)
            redo = [id(edit) for batch in editor.history.redo_stack for edit in batch]
            if noop == "edit":
                editor._apply_batch_edit("")
            else:
                editor.action_unindent()
            assert editor.text == "one\ntwo"
            assert editor._cursor_edits == recorded
            assert [id(edit) for batch in editor.history.redo_stack for edit in batch] == redo
            editor.action_redo()
            assert (editor.text, editor.selection, editor._cursor_preferences) == after

    asyncio.run(exercise())


def test_noop_edit_with_live_undo_keeps_its_original_cursor_snapshot() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "one"
            original = Selection.cursor((0, 0))
            editor._set_selections(original, [], preferences={original: 9})
            editor._apply_batch_edit("X")
            recorded = list(editor._cursor_edits)
            editor._apply_batch_edit("")
            assert editor.text == "Xone"
            assert editor._cursor_edits == recorded
            assert len(recorded) == 1
            assert len(editor.history.undo_stack) == 1
            editor.action_undo()
            assert editor.text == "one"
            assert editor.selection == original
            assert editor.logical_column == 9

    asyncio.run(exercise())


def test_inherited_edit_discards_redo_metadata_during_idle_and_keeps_normal_undo() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            editor = app.active_workspace.editor
            editor.text = "one\ntwo"
            editor._set_selections(Selection((0, 0), (1, 3)), [])
            editor.action_indent()
            editor.action_undo()
            assert editor._cursor_edits
            editor.replace("X", (0, 0), (0, 0))
            editor.history.checkpoint()
            assert not editor.history.redo_stack
            editor.on_idle()
            assert editor._cursor_edits == []
            editor._set_selections(Selection.cursor((1, 2)), [Selection.cursor((0, 1))])
            editor.action_undo()
            assert editor.text == "one\ntwo"
            assert editor.cursor_count == 1
            editor.action_redo()
            assert editor.text == "Xone\ntwo"
            assert editor._cursor_edits == []

    asyncio.run(exercise())
