from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit.sql_explorer import preferences, settings_commands
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.settings import ExplorerSettings, load_settings, save_settings
from analytics_toolkit.sql_explorer.settings_screen import SettingsScreen
from textual.widgets import Button, Checkbox, Input, Select, Static

from tests.sql._support.explorer import FakeSession

if TYPE_CHECKING:
    from pathlib import Path

    from textual.pilot import Pilot


async def _open(app: SqlExplorerApp, pilot: Pilot, command: str = ":settings") -> SettingsScreen:
    field = app.active_workspace.command_input
    field.focus()
    field.value = command
    await pilot.press("enter")
    assert isinstance(app.screen, SettingsScreen)
    return app.screen


async def _press(screen: SettingsScreen, pilot: Pilot, name: str) -> None:
    button = screen.query_one(f"#settings-{name}", Button)
    button.active_effect_duration = 0
    button.focus()
    await pilot.press("enter")


def test_settings_save_applies_all_preferences(tmp_path: Path) -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.settings_path = tmp_path / "settings.json"
        app = SqlExplorerApp(session)
        async with app.run_test(size=(120, 45)) as pilot:
            first = app.active_workspace
            first.editor.text = "select 1"
            await pilot.press("ctrl+t")
            current = app.active_workspace
            current.editor.text = "delete from example"
            screen = await _open(app, pilot)
            screen.query_one("#settings-run-binding", Input).value = " F9 "
            screen.query_one("#settings-confirm", Checkbox).value = False
            screen.query_one("#settings-keyboard", Checkbox).value = True
            screen.query_one("#settings-orientation", Select).value = "vertical"
            screen.query_one("#settings-horizontal-size", Input).value = "12"
            screen.query_one("#settings-vertical-size", Input).value = "48"
            await _press(screen, pilot, "save")
            expected = ExplorerSettings(
                run_binding="f9",
                confirm_mutations=False,
                keyboard_mode=True,
                results_orientation="vertical",
                horizontal_size=12,
                vertical_size=48,
            )
            assert load_settings(session.settings_path).settings == expected
            assert first.session.settings == current.session.settings == expected
            assert app.keyboard_mode
            assert app.focused is current.command_input
            assert current.results_orientation == "vertical"
            assert current.result_sizes == {"horizontal": 12, "vertical": 48}
            assert first.results_orientation == "horizontal"
            assert first.result_sizes == {"horizontal": None, "vertical": None}
            assert first.editor.text == "select 1"
            assert not current.results_open
            await pilot.press("f9")
            await pilot.pause()
            assert len(session.executed) == 1
            assert len(app.screen_stack) == 1  # Confirmation was disabled.
            await pilot.press("f5")
            await pilot.pause()
            assert len(session.executed) == 2
            await pilot.press("ctrl+t")
            assert app.active_workspace.results_orientation == "vertical"
            assert app.active_workspace.result_sizes == current.result_sizes

    asyncio.run(exercise())


def test_saved_settings_restore_and_sizes_fit_a_small_terminal(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    save_settings(
        ExplorerSettings(
            run_binding="f9",
            confirm_mutations=False,
            keyboard_mode=True,
            results_orientation="vertical",
            vertical_size=500,
        ),
        path,
    )

    async def exercise() -> None:
        restored = FakeSession()
        restored.settings = load_settings(path).settings
        again = SqlExplorerApp(restored)
        async with again.run_test(size=(65, 24)) as pilot:
            assert again.keyboard_mode
            screen = await _open(again, pilot, "settings")
            assert screen.query_one("#settings-run-binding", Input).value == "f9"
            assert screen.query_one("#settings-confirm", Checkbox).value is False
            assert screen.query_one("#settings-orientation", Select).value == "vertical"
            await pilot.press("escape")
            again.show_message("Restored layout")
            await pilot.pause()
            pane = again.active_workspace.query_one(".result-pane")
            assert 0 < pane.region.width < again.size.width

    asyncio.run(exercise())


@pytest.mark.parametrize("cancel", ["escape", "cancel"])
def test_reset_is_a_draft_and_cancel_preserves_live_and_saved_state(
    tmp_path: Path, cancel: str
) -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.settings_path = tmp_path / "settings.json"
        session.settings = ExplorerSettings(
            run_binding="f9",
            confirm_mutations=False,
            keyboard_mode=True,
            results_orientation="vertical",
            horizontal_size=10,
            vertical_size=32,
            connections_path="/example/.connections",
        )
        save_settings(session.settings, session.settings_path)
        original = session.settings
        app = SqlExplorerApp(session)
        async with app.run_test(size=(120, 45)) as pilot:
            screen = await _open(app, pilot)
            await _press(screen, pilot, "reset")
            assert screen.query_one("#settings-run-binding", Input).value == "ctrl+enter"
            assert screen.query_one("#settings-horizontal-size", Input).value == ""
            await pilot.press("f8")
            assert app.keyboard_mode  # A modal must not toggle the live preference.
            if cancel == "escape":
                await pilot.press("escape")
            else:
                await _press(screen, pilot, "cancel")
            assert app.session.settings == load_settings(session.settings_path).settings == original
            assert app.active_workspace.results_orientation == "vertical"
            assert app.focused is app.active_workspace.command_input
            screen = await _open(app, pilot)
            await _press(screen, pilot, "reset")
            await _press(screen, pilot, "save")
            expected = replace(ExplorerSettings(), connections_path=original.connections_path)
            assert app.session.settings == load_settings(session.settings_path).settings == expected
            assert not app.keyboard_mode
            assert app.active_workspace.result_sizes == {"horizontal": None, "vertical": None}

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("run-binding", "ctrl+a", "reserved"),
        ("run-binding", "invalid", "Shortcut must"),
        ("horizontal-size", "-1", "Height must"),
        ("vertical-size", "1.5", "Width must"),
        ("vertical-size", "bad", "Width must"),
    ],
)
def test_invalid_values_leave_form_open_without_partial_save(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.settings_path = tmp_path / "settings.json"
        app = SqlExplorerApp(session)
        async with app.run_test(size=(120, 45)) as pilot:
            screen = await _open(app, pilot)
            screen.query_one("#settings-confirm", Checkbox).value = False
            screen.query_one(f"#settings-{field}", Input).value = value
            await _press(screen, pilot, "save")
            assert app.screen is screen
            assert message in str(screen.query_one("#settings-notice", Static).renderable)
            button = screen.query_one("#settings-connections", Button)
            assert button.region.bottom <= screen.query_one("#settings-fields").region.bottom
            assert app.session.settings == ExplorerSettings()
            assert not session.settings_path.exists()

    asyncio.run(exercise())


def test_failed_save_can_retry_without_changing_live_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_save(*args):
        message = "Permission denied"
        raise OSError(message)

    async def exercise() -> None:
        session = FakeSession()
        session.settings_path = tmp_path / "settings.json"
        save_settings(session.settings, session.settings_path)
        app = SqlExplorerApp(session)
        async with app.run_test(size=(120, 45)) as pilot:
            screen = await _open(app, pilot)
            screen.query_one("#settings-run-binding", Input).value = "f9"
            screen.query_one("#settings-keyboard", Checkbox).value = True
            with monkeypatch.context() as patch:
                patch.setattr(preferences, "save_settings", fail_save)
                await _press(screen, pilot, "save")
            assert app.screen is screen
            assert "Permission denied" in str(
                screen.query_one("#settings-notice", Static).renderable
            )
            assert (
                app.session.settings
                == load_settings(session.settings_path).settings
                == ExplorerSettings()
            )
            assert not app.keyboard_mode
            assert app._primary_binding == "ctrl+enter"
            await _press(screen, pilot, "save")
            assert app._primary_binding == "f9"
            assert app.keyboard_mode

    asyncio.run(exercise())


def test_keyboard_navigation_and_connections_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".connections"
    monkeypatch.setattr(settings_commands, "get_connections_path_override", lambda: path)

    async def exercise() -> None:
        session = FakeSession()
        session.settings_path = tmp_path / "settings.json"
        app = SqlExplorerApp(session)
        opened = []

        def connections(arguments):
            assert len(app.screen_stack) == 1
            opened.append(arguments)

        monkeypatch.setattr(app, "_command_connections", connections)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = await _open(app, pilot)
            assert str(path) in str(
                screen.query_one("#settings-connections-path", Static).renderable
            )
            await pilot.press("tab")
            assert app.focused is screen.query_one("#settings-confirm")
            await pilot.press("space", "tab")
            assert app.focused is screen.query_one("#settings-keyboard")
            await pilot.press("up")
            assert app.focused is screen.query_one("#settings-confirm")
            await pilot.press("down")
            assert app.focused is screen.query_one("#settings-keyboard")
            await pilot.press("shift+tab")
            assert app.focused is screen.query_one("#settings-confirm")
            await pilot.press("down", "tab")
            assert app.focused is screen.query_one("#settings-orientation")
            await pilot.press("enter", "down", "enter")
            assert screen.query_one("#settings-orientation", Select).value == "vertical"
            await pilot.press("enter", "up", "escape")
            assert app.screen is screen
            assert not screen.query_one("#settings-orientation", Select).expanded
            await pilot.press("tab", "tab", "tab")
            assert app.focused is screen.query_one("#settings-connections")
            await pilot.press("enter")
            assert opened == [[]]
            assert not load_settings(session.settings_path).settings.confirm_mutations
            assert app.focused is app.active_workspace.command_input

    asyncio.run(exercise())


def test_menu_uses_current_tab_layout_and_rejects_arguments() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(120, 45)) as pilot:
            first = app.active_workspace
            await pilot.press("ctrl+t")
            app._command_results(["switch"])
            await pilot.press("ctrl+shift+tab")
            assert app.active_workspace is first
            app._command_results(["shrink", "1000"])
            screen = await _open(app, pilot)
            assert screen.query_one("#settings-orientation", Select).value == "horizontal"
            assert screen.query_one("#settings-horizontal-size", Input).value == "0"
            app._handle_command(["settings"])
            assert len(app.screen_stack) == 2
            await pilot.press("escape")
            screen = await _open(app, pilot)
            await _press(screen, pilot, "save")
            assert first.session.settings.results_orientation == "horizontal"
            assert first.result_sizes["horizontal"] == 0
            app._handle_command(["settings", "unexpected"])
            assert len(app.screen_stack) == 1
            assert "Usage: settings" in str(first.result_message._message)

    asyncio.run(exercise())
