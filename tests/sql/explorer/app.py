from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pandas as pd
from analytics_toolkit.sql_explorer import app as app_module
from analytics_toolkit.sql_explorer.app import (
    ConfirmMutationScreen,
    DatabasePickerApp,
    ResultMessage,
    ResultTable,
    SqlEditor,
    SqlExplorerApp,
)
from analytics_toolkit.sql_explorer.runtime import ExplorerCancelResult, ExplorerRunResult
from analytics_toolkit.sql_explorer.settings import ExplorerSettings
from analytics_toolkit.sql_explorer.statements import build_execution_plan
from textual.widgets import Input, Static

if TYPE_CHECKING:
    import pytest


class FakeSession:
    def __init__(self) -> None:
        self.settings = ExplorerSettings()
        self.settings_warning: str | None = None
        self.database = SimpleNamespace(connection_key="gp", backend="gp")
        self.executed: list[Any] = []
        self.cancel_calls = 0

    def plan(self, sql_text: str) -> Any:
        return build_execution_plan(sql_text, self.database.backend)

    def execute(self, plan: Any) -> ExplorerRunResult:
        self.executed.append(plan)
        return ExplorerRunResult(
            route=plan.route,
            dataframe=pd.DataFrame({"value": [1, None]}),
            displayed_rows=2,
            total_rows=2,
            truncated=False,
            status="Returned 2 row(s).",
        )

    def switch_database(self, key: str) -> Any:
        if key == "bad":
            message = "bad database"
            raise ValueError(message)
        self.database = SimpleNamespace(connection_key=key, backend="trino")
        return self.database

    def set_run_binding(self, value: str) -> ExplorerSettings:
        binding = "ctrl+enter" if value == "reset" else value
        self.settings = replace(self.settings, run_binding=binding)
        return self.settings

    def set_confirmation(self, *, enabled: bool) -> ExplorerSettings:
        self.settings = replace(self.settings, confirm_mutations=enabled)
        return self.settings

    def cancel_active(self) -> ExplorerCancelResult:
        self.cancel_calls += 1
        return ExplorerCancelResult(1, 1, "Cancellation requested for 1 query.")


class ErrorSession(FakeSession):
    def execute(self, plan: Any) -> ExplorerRunResult:
        message = plan.execution_sql
        raise RuntimeError(message)


class CancelErrorSession(FakeSession):
    def cancel_active(self) -> ExplorerCancelResult:
        message = "cancel failed"
        raise RuntimeError(message)


class NoRowsSession(FakeSession):
    def execute(self, plan: Any) -> ExplorerRunResult:
        return ExplorerRunResult(
            route=plan.route,
            dataframe=None,
            displayed_rows=0,
            total_rows=None,
            truncated=False,
            status="Executed successfully.",
        )


def test_database_picker_selects_highlighted_key() -> None:
    async def exercise() -> None:
        application = DatabasePickerApp((("gp", "gp"), ("lake", "trino")))
        async with application.run_test() as pilot:
            await pilot.press("down", "enter")
        assert application.return_value == "lake"

    asyncio.run(exercise())


def test_database_picker_can_be_cancelled() -> None:
    async def exercise() -> None:
        application = DatabasePickerApp((("gp", "gp"),))
        async with application.run_test() as pilot:
            application.on_option_list_option_selected(SimpleNamespace(option_id=None))
            await pilot.press("escape")
        assert application.return_value is None

    asyncio.run(exercise())


def test_mount_and_tab_cycle_visible_workspaces() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(100, 35)) as pilot:
            editor = application.query_one(SqlEditor)
            command = application.query_one("#command-input", Input)
            assert application.focused is editor
            status = application.query_one("#session-status", Static)
            assert "mode=exploratory" in str(status.render())

            await pilot.press("alt+tab")
            assert application.focused is command
            await pilot.press("alt+shift+tab")
            assert application.focused is editor

            application.show_dataframe(pd.DataFrame({"a": [1], "b": [None]}))
            await pilot.press("alt+tab")
            assert isinstance(application.focused, ResultTable)
            await pilot.press("delete")
            assert application.results_open is False
            assert application.focused is editor

    asyncio.run(exercise())


def test_vertical_arrows_cross_only_workspace_boundaries() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(100, 35)) as pilot:
            editor = application.query_one(SqlEditor)
            command = application.query_one("#command-input", Input)
            editor.text = "one\ntwo"
            application.show_dataframe(pd.DataFrame({"value": [1, 2]}))
            table = application.query_one(ResultTable)

            assert editor.show_line_numbers is True
            editor.focus()
            editor.cursor_location = (0, 0)
            await pilot.press("up")
            assert application.focused is command

            await pilot.press("up")
            assert application.focused is table
            await pilot.press("up")
            assert application.focused is editor

            await pilot.press("down")
            assert editor.cursor_location[0] == 1
            assert application.focused is editor
            await pilot.press("up")
            assert editor.cursor_location[0] == 0
            await pilot.press("down")
            await pilot.press("down")
            assert application.focused is table

            await pilot.press("down")
            assert table.cursor_row == 1
            await pilot.press("up")
            assert table.cursor_row == 0
            await pilot.press("down")
            await pilot.press("down")
            assert application.focused is command
            await pilot.press("down")
            assert application.focused is editor

    asyncio.run(exercise())


def test_home_and_end_use_line_edges_and_shift_controls_selection() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "  select 1\nfrom sample"
            editor.cursor_location = (0, 5)

            await pilot.press("home")
            assert editor.cursor_location == (0, 0)
            assert editor.selected_text == ""

            editor.cursor_location = (0, 2)
            await pilot.press("shift+home")
            assert editor.cursor_location == (0, 0)
            assert editor.selected_text == "  "

            await pilot.press("end")
            assert editor.cursor_location == (0, len("  select 1"))
            assert editor.selected_text == ""

            editor.cursor_location = (1, 0)
            await pilot.press("shift+end")
            assert editor.cursor_location == (1, len("from sample"))
            assert editor.selected_text == "from sample"

    asyncio.run(exercise())


def test_standard_editor_cut_copy_paste_undo_and_redo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clipboard: list[str] = []
    monkeypatch.setattr(app_module.pyperclip, "copy", clipboard.append)
    monkeypatch.setattr(app_module.pyperclip, "paste", lambda: clipboard[-1])

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "select 1"
            editor.cursor_location = (0, 0)
            await pilot.press("tab")
            assert editor.text == "    select 1"
            await pilot.press("shift+tab")
            assert editor.text == "select 1"
            await pilot.press("ctrl+a", "ctrl+c", "ctrl+x")
            assert clipboard[-1] == "select 1"
            assert editor.text == ""
            await pilot.press("ctrl+v")
            assert editor.text == "select 1"
            await pilot.press("ctrl+z")
            assert editor.text == ""
            await pilot.press("ctrl+y")
            assert editor.text == "select 1"

    asyncio.run(exercise())


def test_run_shortcuts_execute_complete_editor_and_show_results() -> None:
    async def exercise() -> None:
        session = FakeSession()
        application = SqlExplorerApp(session)
        async with application.run_test() as pilot:
            application.query_one(SqlEditor).text = "select 1"
            await pilot.press("ctrl+enter")
            await pilot.pause()

            assert len(session.executed) == 1
            assert application.results_open is True
            assert application.query_one(ResultTable).row_count == 2
            notice = application.query_one("#notice", Static)
            assert "Returned 2 row(s)." in str(notice.render())

            application.close_results()
            await pilot.press("f5")
            await pilot.pause()
            assert len(session.executed) == 2

            application.close_results()
            for run_key in (
                "kp_enter",
                "hyper+enter",
                "meta+enter",
                "super+enter",
            ):
                await pilot.press(run_key)
                await pilot.pause()
                application.close_results()
            assert len(session.executed) == 6

    asyncio.run(exercise())


def test_mutation_requires_confirmation() -> None:
    async def exercise() -> None:
        session = FakeSession()
        application = SqlExplorerApp(session)
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "delete from sample"
            await pilot.press("f5")
            assert isinstance(application.screen, ConfirmMutationScreen)
            assert session.executed == []

            await pilot.press("y")
            await pilot.pause()
            assert len(session.executed) == 1

    asyncio.run(exercise())


def test_mutation_confirmation_can_be_cancelled_and_buttons_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        session = FakeSession()
        application = SqlExplorerApp(session)
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "delete from sample"
            await pilot.press("f5", "n")
            assert session.executed == []

            editor.text = "delete from sample where value = '" + ("x" * 2_100) + "'"
            await pilot.press("f5")
            await pilot.click("#confirm-cancel")
            await pilot.pause()
            assert session.executed == []

        screen = ConfirmMutationScreen(
            build_execution_plan("delete from sample", "gp"),
            db_key="gp",
            backend="gp",
        )
        dismissed: list[bool] = []
        monkeypatch.setattr(screen, "dismiss", dismissed.append)
        screen.on_button_pressed(SimpleNamespace(button=SimpleNamespace(id="confirm-execute")))
        assert dismissed == [True]

    asyncio.run(exercise())


def test_commands_update_session_and_workspace() -> None:
    async def exercise() -> None:
        session = FakeSession()
        application = SqlExplorerApp(session)
        async with application.run_test() as pilot:
            command = application.query_one("#command-input", Input)
            editor = application.query_one(SqlEditor)
            editor.text = "select 1"

            for command_text in ("db warehouse", "shortcut f8", "confirm off", "mode"):
                command.value = command_text
                command.focus()
                await pilot.press("enter")

            assert session.database.connection_key == "warehouse"
            assert session.settings.run_binding == "f8"
            assert session.settings.confirm_mutations is False
            notice = application.query_one("#notice", Static)
            assert "Current mode: exploratory" in str(notice.render())

            command.value = ":clear all"
            await pilot.press("enter")
            assert editor.text == ""
            assert application.results_open is False

    asyncio.run(exercise())


def test_help_and_errors_use_closable_result_message() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            command = application.query_one("#command-input", Input)
            command.value = "help"
            command.focus()
            await pilot.press("enter")
            message = application.query_one(ResultMessage)
            assert application.results_open is True
            assert "Commands" in str(message.render())

            message.focus()
            await pilot.press("delete")
            assert application.results_open is False

            command.value = "unknown"
            command.focus()
            await pilot.press("enter")
            assert "Unknown command" in str(message.render())

    asyncio.run(exercise())


def test_cancel_command_interrupts_busy_operation() -> None:
    async def exercise() -> None:
        session = FakeSession()
        application = SqlExplorerApp(session)
        async with application.run_test() as pilot:
            application.busy = True
            command = application.query_one("#command-input", Input)
            command.value = "cancel"
            command.focus()
            await pilot.press("enter")
            await pilot.pause()

            assert session.cancel_calls == 1
            assert application.cancelling is False
            notice = application.query_one("#notice", Static)
            assert "Cancellation requested" in str(notice.render())

    asyncio.run(exercise())


def test_exit_cancels_busy_operation_before_closing() -> None:
    async def exercise() -> None:
        session = FakeSession()
        application = SqlExplorerApp(session)
        async with application.run_test() as pilot:
            application.busy = True
            command = application.query_one("#command-input", Input)
            command.value = "exit"
            command.focus()
            await pilot.press("enter")
            await pilot.pause()
        assert session.cancel_calls == 1

    asyncio.run(exercise())


def test_run_and_cancel_worker_errors_are_rendered() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(ErrorSession())
        async with application.run_test() as pilot:
            application.query_one(SqlEditor).text = "select 1"
            await pilot.press("f5")
            await pilot.pause()
            assert "RuntimeError" in str(application.query_one(ResultMessage).render())

        cancel_application = SqlExplorerApp(CancelErrorSession())
        async with cancel_application.run_test() as pilot:
            cancel_application.busy = True
            command = cancel_application.query_one("#command-input", Input)
            command.value = "cancel"
            command.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert "cancel failed" in str(cancel_application.query_one(ResultMessage).render())

    asyncio.run(exercise())


def test_no_result_execution_closes_result_pane() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(NoRowsSession())
        async with application.run_test() as pilot:
            application.show_message("old")
            application.query_one(SqlEditor).text = "delete from sample"
            application.session.settings = replace(
                application.session.settings,
                confirm_mutations=False,
            )
            await pilot.press("f5")
            await pilot.pause()
            assert application.results_open is False

    asyncio.run(exercise())


def test_mount_warning_busy_run_and_invalid_editor_are_reported() -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.settings_warning = "settings warning"
        application = SqlExplorerApp(session)
        async with application.run_test() as pilot:
            notice = application.query_one("#notice", Static)
            assert "settings warning" in str(notice.render())

            application.busy = True
            application.action_run_query()
            assert "already running" in str(notice.render())

            application.busy = False
            application.query_one(SqlEditor).text = ""
            application.action_run_query()
            assert "Enter a SQL statement" in str(application.query_one(ResultMessage).render())
            await pilot.pause()

    asyncio.run(exercise())


def test_command_validation_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        session = FakeSession()
        application = SqlExplorerApp(session)
        async with application.run_test() as pilot:
            command = application.query_one("#command-input", Input)
            application.action_plain_tab()
            application.action_plain_shift_tab()
            application.action_copy_focused()
            application._handle_command([])
            application.on_input_submitted(
                SimpleNamespace(input=SimpleNamespace(id="other"), value="ignored")
            )

            command.value = "   "
            command.focus()
            await pilot.press("enter")
            command.value = 'help "unterminated'
            await pilot.press("enter")

            application._command_run(["extra"])
            application._command_mode(["readonly"])
            application._command_mode(["exploratory"])
            application._command_help(["extra"])
            application._command_exit(["extra"])
            application._command_cancel(["extra"])
            application._command_database([])
            application._command_shortcut([])
            application._command_confirmation([])
            application._command_confirmation(["on"])
            application._command_clear([])

            application.busy = True
            application._command_database(["warehouse"])
            application.busy = False
            application._command_database(["bad"])

            monkeypatch.setattr(
                session,
                "set_run_binding",
                lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
            application._command_shortcut(["f8"])
            monkeypatch.setattr(
                session,
                "set_confirmation",
                lambda **kwargs: (_ for _ in ()).throw(OSError("settings failed")),
            )
            application._command_confirmation(["toggle"])

            await pilot.pause()

    asyncio.run(exercise())


def test_workspace_and_editor_defensive_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "select 1"
            application.show_message("result")
            application._command_clear(["query"])
            application.show_message("result")
            application._command_clear(["results"])

            application._request_cancel(exit_after=False)
            application.busy = True
            application.cancelling = True
            application._exit_after_cancel = False
            application._request_cancel(exit_after=False)
            application._request_cancel(exit_after=True)

            application._install_primary_binding("f5")
            application._install_primary_binding("ctrl+enter")
            other_focus = Input(id="other-focus")
            await application.screen.mount(other_focus)
            other_focus.focus()
            await pilot.pause()
            assert application.focused is other_focus
            application._focus_relative(1)
            other_focus.focus()
            await pilot.pause()
            application._focus_relative(-1)

            editor.focus()
            editor.text = "one\ntwo"
            editor.action_cursor_document_end()
            editor.action_cursor_document_start()
            editor.action_cut()
            monkeypatch.setattr(application, "paste_from_explorer_clipboard", lambda: "")
            editor.action_paste()
            editor.cursor_location = (0, 0)
            editor.action_unindent()
            await pilot.pause()

    asyncio.run(exercise())


def test_remaining_app_navigation_and_command_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            command = application.query_one("#command-input", Input)
            command.focus()
            await pilot.pause()
            application.action_plain_tab()
            application.action_plain_shift_tab()

            application.busy = True
            application._command_run([])
            application.busy = False
            application.on_text_area_changed(SimpleNamespace(text_area=object()))

            exits: list[bool] = []
            monkeypatch.setattr(application, "exit", lambda: exits.append(True))
            application._request_cancel(exit_after=True)
            assert exits == [True]

            application.show_message("error")
            message = application.query_one(ResultMessage)
            message.focus()
            await pilot.press("up")
            assert isinstance(application.focused, SqlEditor)
            message.focus()
            await pilot.press("down")
            assert application.focused is command

    asyncio.run(exercise())


def test_idle_exit_and_failed_cancel_exit_are_available() -> None:
    async def exercise() -> None:
        idle_application = SqlExplorerApp(FakeSession())
        async with idle_application.run_test() as pilot:
            command = idle_application.query_one("#command-input", Input)
            command.value = "exit"
            command.focus()
            await pilot.press("enter")

        cancel_application = SqlExplorerApp(CancelErrorSession())
        async with cancel_application.run_test() as pilot:
            cancel_application.busy = True
            command = cancel_application.query_one("#command-input", Input)
            command.value = "exit"
            command.focus()
            await pilot.press("enter")
            await pilot.pause()

    asyncio.run(exercise())


def test_clipboard_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_clipboard(*args: Any) -> str:
        message = "unavailable"
        raise app_module.pyperclip.PyperclipException(message)

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        monkeypatch.setattr(app_module.pyperclip, "copy", fail_clipboard)
        monkeypatch.setattr(app_module.pyperclip, "paste", fail_clipboard)
        application.copy_to_explorer_clipboard("fallback")
        assert application.paste_from_explorer_clipboard() == "fallback"

        monkeypatch.setattr(app_module.pyperclip, "paste", lambda: "")
        assert application.paste_from_explorer_clipboard() == "fallback"

    asyncio.run(exercise())


def test_cell_formatting_handles_nulls_newlines_and_long_values() -> None:
    assert app_module._format_cell(None) == "NULL"
    assert app_module._format_cell(float("nan")) == "NULL"
    assert app_module._format_cell("a\nb") == "a\\nb"
    assert app_module._format_cell("x" * 600).endswith("…")
    assert app_module._format_cell([1, 2]) == "[1, 2]"
    assert app_module._format_cell(Decimal("20778982.000000000000")) == "20778982"
    assert app_module._format_cell(Decimal("123.4500")) == "123.45"
    assert app_module._format_cell(Decimal("-0.000")) == "0"
