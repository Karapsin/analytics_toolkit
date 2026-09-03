from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

from analytics_toolkit.sql_explorer import widgets as widgets_module
from analytics_toolkit.sql_explorer.app import ResultMessage, SqlEditor, SqlExplorerApp
from analytics_toolkit.sql_explorer.filetree import read_sql_file, safe_entries
from analytics_toolkit.sql_explorer.widgets import (
    DiscardChangesScreen,
    FileNavigationScreen,
    SqlFileTree,
)
from textual.widgets import Input, Static

from tests.sql.explorer.app import FakeSession

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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

    assert [entry.name for entry in entries] == ["Alpha", "zeta", "A.sql", "b.SQL"]
    assert read_sql_file(tmp_path / "A.sql") == "select 1"


def test_safe_entries_keeps_internal_symlinks_and_skips_filesystem_faults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.sql"
    target.write_text("select 1", encoding="utf-8")
    (tmp_path / "inside.sql").symlink_to(target)
    (tmp_path / "broken.sql").symlink_to(target)
    (tmp_path / "fault.sql").write_text("select 2", encoding="utf-8")

    path_type = type(tmp_path)
    original_resolve = path_type.resolve
    original_is_dir = path_type.is_dir

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

    monkeypatch.setattr(path_type, "resolve", resolve)
    monkeypatch.setattr(path_type, "is_dir", is_dir)

    assert [entry.name for entry in safe_entries(tmp_path)] == [
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
            assert isinstance(application.focused, SqlFileTree)

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

            await pilot.press("down", "enter")
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
        async with application.run_test():
            screen = FileNavigationScreen(tmp_path)
            await application.push_screen(screen)
            tree = screen.query_one(SqlFileTree)
            directory_node = tree.root.children[0]
            tree.on_tree_node_expanded(SimpleNamespace(node=directory_node))
            assert [node.label.plain for node in directory_node.children] == ["nested.sql"]

            screen.on_tree_node_selected(
                SimpleNamespace(node=SimpleNamespace(data=child), stop=lambda: None)
            )
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
