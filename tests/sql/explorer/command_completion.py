from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.command_completion import command_suggestions
from analytics_toolkit.sql_explorer.panes import CommandInput
from analytics_toolkit.sql_explorer.runtime import ExplorerSession

from tests.sql.explorer.app import FakeSession


@pytest.mark.parametrize(
    ("value", "cursor", "expected"),
    [
        (":cre", 4, ("create_table",)),
        ("  :cre", 6, ("create_table",)),
        ("confirm o", 9, ("on", "off")),
        ("mode ", 5, ("exploratory", "navigation")),
        ("db wa", 5, ("warehouse", "warehouse_dev")),
        (":db wa", 6, ("warehouse", "warehouse_dev")),
        ("db warehouse x", 14, ()),
        ("create_table", 3, ()),
        ("unknown ", 8, ()),
    ],
)
def test_command_prefixes(value: str, cursor: int, expected: tuple[str, ...]) -> None:
    _, suggestions = command_suggestions(value, cursor, ("warehouse", "warehouse_dev", "local"))
    assert suggestions == expected


def test_database_completion_only_validates_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def validate(*, connect: bool) -> list[SimpleNamespace]:
        calls.append(connect)
        return [
            SimpleNamespace(connection_key="warehouse", valid=True, backend="gp"),
            SimpleNamespace(connection_key="broken", valid=False, backend="gp"),
            SimpleNamespace(connection_key="missing", valid=True, backend=None),
        ]

    monkeypatch.setattr(sql, "validate_connections", validate)
    assert ExplorerSession.database_keys(object()) == ("warehouse",)
    assert calls == [False]


def test_command_menu_keyboard_filters_and_accepts_without_running() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            command = app.query_one("#command-input", CommandInput)
            command.focus()
            await pilot.press("c", "tab")
            assert command.completion_menu.option_count > 1
            await pilot.press("r")
            assert command.completion_menu.option_count == 1
            await pilot.press("enter")
            assert command.value == "create_table"
            assert not command.completion_menu.display
            assert len(app.screen_stack) == 1
            command.value = "confirm "
            command.cursor_position = len(command.value)
            await pilot.press("tab", "down", "up", "down", "enter")
            assert command.value == "confirm off"
            assert app.session.settings.confirm_mutations
            command.value = "c"
            command.cursor_position = 1
            await pilot.press("tab", "escape")
            assert not command.completion_menu.display
            assert app.focused is command

    asyncio.run(exercise())


def test_db_key_menu_filters_and_preserves_database_until_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FakeSession,
        "database_keys",
        lambda _: ("warehouse", "warehouse_dev", "local"),
        raising=False,
    )

    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            command = app.query_one("#command-input", CommandInput)
            command.focus()
            command.value = ":db wa"
            command.cursor_position = len(command.value)
            await pilot.press("tab")
            assert command.completion_menu.option_count == 2
            await pilot.press("down", "enter")
            assert command.value == ":db warehouse_dev"
            assert app.session.database.connection_key == "gp"
            await pilot.press("enter")
            assert app.session.database.connection_key == "warehouse_dev"

    asyncio.run(exercise())


def test_command_completion_accepts_tab_and_handles_configuration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(_: object) -> tuple[str, ...]:
        message = "invalid configuration"
        raise ValueError(message)

    monkeypatch.setattr(FakeSession, "database_keys", broken, raising=False)

    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            command = app.query_one("#command-input", CommandInput)
            command.focus()
            await pilot.press("c", "tab", "tab")
            assert command.value == "cancel"
            command.value = "cre"
            command.cursor_position = 3
            await pilot.press("ctrl+space")
            assert command.value == "create_table"
            command.value = "c"
            command.cursor_position = 1
            await pilot.press("tab", "left")
            assert not command.completion_menu.display
            command.value = "c"
            command.cursor_position = 1
            await pilot.press("tab")
            command.value = "unknown"
            command.cursor_position = len(command.value)
            command.accept_completion()
            assert command.value == "unknown"
            command.completion_menu.highlighted = None
            command.accept_completion()
            command.value = "db "
            command.cursor_position = 3
            await pilot.press("tab")
            assert "invalid configuration" in str(app.active_workspace.result_message.render())
            assert not command.completion_menu.display

    asyncio.run(exercise())
