from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit.sql_explorer import widgets as widgets_module
from analytics_toolkit.sql_explorer.app import ResultMessage, SqlEditor, SqlExplorerApp
from analytics_toolkit.sql_explorer.filetree import (
    completion_entries,
    read_sql_file,
    safe_entries,
)
from analytics_toolkit.sql_explorer.widgets import (
    DiscardChangesScreen,
    FileNavigationScreen,
    SqlFileTree,
)
from textual.widgets import Input, Static

from tests.sql.explorer.app import FakeSession

if TYPE_CHECKING:
    from pathlib import Path


def test_safe_entries_orders_directories_then_sql_and_filters_unsafe_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "zeta").mkdir()
    (tmp_path / "Alpha").mkdir()
    (tmp_path / "b.SQL").write_text("select 2", encoding="utf-8")
    (tmp_path / "A.sql").write_text("select 1", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("hidden", encoding="utf-8")
    (tmp_path / ".hidden.sql").write_text("hidden", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.sql"
    outside.write_text("select 3", encoding="utf-8")
    (tmp_path / "outside.sql").symlink_to(outside)

    entries = safe_entries(tmp_path)

    names = [entry.name for entry in entries]
    assert names[:2] == ["Alpha", "zeta"]
    assert {".connections", ".hidden.sql", "A.sql", "b.SQL", "notes.txt"} <= set(names)
    assert "outside.sql" not in names
    assert read_sql_file(tmp_path / "A.sql") == "select 1"

    directory, matches = completion_entries(tmp_path, "a")
    assert directory == tmp_path.resolve()
    assert [path.name for path in matches] == ["Alpha", "A.sql"]
    absolute_directory, _ = completion_entries(tmp_path, str(tmp_path / "A"))
    assert absolute_directory == tmp_path.resolve()
    with pytest.raises(ValueError, match="must remain inside"):
        completion_entries(tmp_path, "../outside")


def test_safe_entries_keeps_internal_symlinks_and_skips_filesystem_faults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.sql"
    target.write_text("select 1", encoding="utf-8")
    (tmp_path / "inside.sql").symlink_to(target)
    (tmp_path / "broken.sql").symlink_to(target)
    (tmp_path / "fault.sql").write_text("select 2", encoding="utf-8")
    (tmp_path / "ignored").write_text("ignored", encoding="utf-8")

    path_type = type(tmp_path)
    original_resolve = path_type.resolve
    original_is_dir = path_type.is_dir
    original_is_file = path_type.is_file

    def resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path.name == "broken.sql":
            message = "cannot resolve"
            raise OSError(message)
        return original_resolve(path, *args, **kwargs)

    def is_dir(path: Path) -> bool:
        if path.name == "fault.sql":
            message = "cannot stat"
            raise OSError(message)
        return original_is_dir(path)

    def is_file(path: Path) -> bool:
        if path.name == "ignored":
            return False
        return original_is_file(path)

    monkeypatch.setattr(path_type, "resolve", resolve)
    monkeypatch.setattr(path_type, "is_dir", is_dir)
    monkeypatch.setattr(path_type, "is_file", is_file)

    assert [entry.name for entry in safe_entries(tmp_path)] == [
        ".connections",
        "inside.sql",
        "target.sql",
    ]


def test_navigation_is_a_remote_cwd_mode_not_a_default_pane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    expected_root = tmp_path.resolve()

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            assert list(application.query(SqlFileTree)) == []

            await pilot.press("ctrl+o")
            assert isinstance(application.screen, FileNavigationScreen)
            assert application.screen.root_path == expected_root
            path_input = application.screen.query_one("#navigation-path", Input)
            assert application.focused is path_input
            assert path_input.cursor_blink is False

            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(application.focused, SqlEditor)

            command = application.query_one("#command-input", Input)
            command.value = "mode navigation"
            command.focus()
            await pilot.press("enter")
            assert isinstance(application.screen, FileNavigationScreen)
            await pilot.press("escape")

    asyncio.run(exercise())


def test_navigation_path_input_completes_and_descends_to_sql_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "projects" / "one" / "query.sql"
    target.parent.mkdir(parents=True)
    target.write_text("select 42", encoding="utf-8")
    (tmp_path / "projects" / "other").mkdir()
    (tmp_path / "notes.txt").write_text("context", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            await pilot.press("ctrl+o")
            screen = application.screen
            assert isinstance(screen, FileNavigationScreen)
            path_input = screen.query_one("#navigation-path", Input)
            tree = screen.query_one(SqlFileTree)
            assert {"projects", "notes.txt", ".connections"} <= {
                node.label.plain for node in tree.root.children
            }

            await pilot.press("p", "r", "o", "tab")
            assert path_input.value == "projects/"
            await pilot.press("o")
            assert [node.label.plain for node in tree.root.children] == ["one", "other"]
            await pilot.press("tab")
            assert tree.cursor_node is not None
            assert tree.cursor_node.label.plain == "one"
            await pilot.press("tab")
            assert tree.cursor_node is not None
            assert tree.cursor_node.label.plain == "other"
            await pilot.press("shift+tab")
            assert tree.cursor_node is not None
            assert tree.cursor_node.label.plain == "one"
            await pilot.press("n", "tab")
            assert path_input.value == "projects/one/"
            await pilot.press("q", "tab")
            assert path_input.value == "projects/one/query.sql"
            assert isinstance(application.screen, FileNavigationScreen)

            await pilot.press("enter")
            await pilot.pause()
            assert application.query_one(SqlEditor).text == "select 42"

    asyncio.run(exercise())


def test_navigation_shows_non_sql_files_but_refuses_to_open_them(tmp_path: Path) -> None:
    (tmp_path / ".hidden.txt").write_text("hidden", encoding="utf-8")

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            await application.push_screen(FileNavigationScreen(tmp_path))
            screen = application.screen
            assert isinstance(screen, FileNavigationScreen)
            path_input = screen.query_one("#navigation-path", Input)
            assert ".hidden.txt" in {
                node.label.plain for node in screen.query_one(SqlFileTree).root.children
            }

            path_input.value = ".hidden"
            path_input.cursor_position = len(path_input.value)
            await pilot.press("tab", "enter")
            assert isinstance(application.screen, FileNavigationScreen)
            assert "Only .sql files" in str(screen.query_one("#navigation-notice").render())
            await pilot.press("escape")

    asyncio.run(exercise())


def test_open_command_and_keyboard_navigation_load_utf8_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql_file = tmp_path / "query.sql"
    sql_file.write_text("select 'Привет'", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            command = application.query_one("#command-input", Input)
            command.value = "open"
            command.focus()
            await pilot.press("enter")
            assert isinstance(application.screen, FileNavigationScreen)

            await pilot.press("q", "tab", "enter")
            await pilot.pause()
            editor = application.query_one(SqlEditor)
            assert editor.text == "select 'Привет'"
            status = str(application.query_one("#session-status", Static).render())
            assert str(sql_file.resolve()) in status

    asyncio.run(exercise())


def test_dirty_buffer_requires_explicit_discard_before_file_replacement(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.sql"
    second = tmp_path / "second.sql"
    first.write_text("select 1", encoding="utf-8")
    second.write_text("select 2", encoding="utf-8")

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            application.load_sql_file(first)
            editor = application.query_one(SqlEditor)
            editor.text = "select changed"

            application.load_sql_file(second)
            assert isinstance(application.screen, DiscardChangesScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert editor.text == "select changed"

            application.load_sql_file(second)
            await pilot.press("y")
            await pilot.pause()
            assert editor.text == "select 2"

    asyncio.run(exercise())


def test_file_decode_and_directory_errors_use_result_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = tmp_path / "invalid.sql"
    invalid.write_bytes(b"\xff")

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            application.load_sql_file(invalid)
            assert "UnicodeDecodeError" in str(application.query_one(ResultMessage).render())

            def fail_entries(*_args: object, **_kwargs: object) -> list[Path]:
                message = "not allowed"
                raise PermissionError(message)

            monkeypatch.setattr(widgets_module, "safe_entries", fail_entries)
            application.action_open_navigation()
            await pilot.pause()
            assert "PermissionError" in str(application.query_one(ResultMessage).render())

    asyncio.run(exercise())


def test_navigation_widget_defensive_events_and_discard_buttons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = tmp_path / "child"
    child.mkdir()
    (child / "nested.sql").write_text("select 1", encoding="utf-8")

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            screen = FileNavigationScreen(tmp_path)
            await application.push_screen(screen)
            screen.on_tree_node_selected(
                SimpleNamespace(node=SimpleNamespace(data=child), stop=lambda: None)
            )
            await pilot.pause()
            assert screen.is_active
            screen.on_input_changed(
                SimpleNamespace(input=SimpleNamespace(id="other", value="ignored"))
            )
            screen.on_tree_node_selected(SimpleNamespace(node=SimpleNamespace(data=object())))

            path_input = screen.query_one("#navigation-path", Input)
            path_input.value = "missing/child"
            await pilot.pause()
            screen.action_complete_path()
            screen.action_choose_path()
            screen.action_next_match()
            assert screen.is_active

            path_input.value = ""
            screen._matches = ()
            screen.action_complete_path()
            assert screen._matches

            path_type = type(tmp_path)
            original_is_dir = path_type.is_dir

            def fail_is_dir(path: Path) -> bool:
                if path.name == "fault":
                    message = "cannot inspect"
                    raise PermissionError(message)
                return original_is_dir(path)

            monkeypatch.setattr(path_type, "is_dir", fail_is_dir)
            screen._matches = (tmp_path / "fault",)
            screen.action_complete_path()
            screen._choose_entry(tmp_path / "fault")
            assert "PermissionError" in str(screen.query_one("#navigation-notice").render())
            assert screen.is_active
            screen.dismiss(None)

        inactive = FileNavigationScreen(tmp_path)
        error = SqlFileTree.DirectoryError(PermissionError("closed"))
        inactive.on_sql_file_tree_directory_error(error)

        screen = DiscardChangesScreen(tmp_path / "next.sql")
        dismissed: list[bool] = []
        monkeypatch.setattr(screen, "dismiss", dismissed.append)
        screen.on_button_pressed(SimpleNamespace(button=SimpleNamespace(id="discard-confirm")))
        screen.on_button_pressed(SimpleNamespace(button=SimpleNamespace(id="discard-cancel")))
        assert dismissed == [True, False]

    asyncio.run(exercise())
