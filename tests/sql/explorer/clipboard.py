from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING, Any

import pandas as pd
from analytics_toolkit.sql_explorer import app as app_module
from analytics_toolkit.sql_explorer.app import ResultTable, SqlEditor, SqlExplorerApp
from analytics_toolkit.sql_explorer.clipboard import TerminalClipboard, osc52_sequence
from textual.document._document import Selection

from tests.sql.explorer.app import FakeSession

if TYPE_CHECKING:
    import pytest


class RecordingWriter:
    def __init__(self) -> None:
        self.output: list[str] = []
        self.flushes = 0

    def write(self, value: str) -> None:
        self.output.append(value)

    def flush(self) -> None:
        self.flushes += 1


def test_osc52_sequence_encodes_utf8_and_terminal_writer_flushes() -> None:
    value = "remote π clipboard"
    sequence = osc52_sequence(value)
    assert sequence.startswith("\x1b]52;c;")
    assert sequence.endswith("\x07")
    encoded = sequence[len("\x1b]52;c;") : -1]
    assert base64.b64decode(encoded).decode("utf-8") == value

    writer = RecordingWriter()
    clipboard = TerminalClipboard(writer=writer)
    clipboard.copy(value)
    assert writer.output == [sequence]
    assert writer.flushes == 1
    assert clipboard.fallback == value


def test_terminal_clipboard_callable_writer_and_explicit_flush_paths() -> None:
    output: list[str] = []
    flushes: list[bool] = []
    clipboard = TerminalClipboard(writer=output.append, flush=lambda: flushes.append(True))
    clipboard.copy("explicit")
    assert output == [osc52_sequence("explicit")]
    assert flushes == [True]

    no_flush_output: list[str] = []
    no_flush = TerminalClipboard(writer=no_flush_output.append)
    no_flush.copy("memory")
    assert no_flush_output == [osc52_sequence("memory")]
    assert no_flush.paste_fallback() == "memory"


def test_app_copy_order_and_unavailable_clipboard_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        events: list[str] = []
        application = SqlExplorerApp(FakeSession())
        application._terminal_clipboard = type(
            "Terminal",
            (),
            {"copy": lambda _self, value: events.append(f"osc:{value}")},
        )()
        monkeypatch.setattr(
            app_module.pyperclip,
            "copy",
            lambda value: events.append(f"pyperclip:{value}"),
        )

        application.copy_to_explorer_clipboard("chosen")

        assert events == ["osc:chosen", "pyperclip:chosen"]
        assert application._clipboard == "chosen"

        def unavailable(*_args: Any) -> str:
            message = "unavailable"
            raise app_module.pyperclip.PyperclipException(message)

        monkeypatch.setattr(app_module.pyperclip, "paste", unavailable)
        assert application.paste_from_explorer_clipboard() == "chosen"

    asyncio.run(exercise())


def test_result_copy_uses_raw_values_for_cells_and_rectangles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        copied: list[str] = []
        application = SqlExplorerApp(FakeSession())
        monkeypatch.setattr(application, "copy_to_explorer_clipboard", copied.append)
        async with application.run_test() as pilot:
            application.show_dataframe(
                pd.DataFrame({"number": [1000, -2000], "text": ["1,000", "x" * 600]})
            )
            table = application.active_workspace.result_table
            table.focus()
            await pilot.press("ctrl+c")
            assert copied[-1] == "1000"
            table.set_cell_selection(0, 0)
            table.set_cell_selection(1, 1, extend=True)
            await pilot.press("ctrl+c")
            assert copied[-1] == "1000\t1,000\n-2000\t" + "x" * 600
            application.show_dataframe(pd.DataFrame({"number": [3000.25]}))
            table.focus()
            await pilot.press("ctrl+c")
            assert copied[-1] == "3000.25"

    asyncio.run(exercise())


def test_ctrl_c_copies_editor_range_and_selected_result_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copied: list[str] = []
    monkeypatch.setattr(
        SqlExplorerApp,
        "copy_to_explorer_clipboard",
        lambda _self, value: copied.append(value),
    )

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "select chosen"
            editor.selection = Selection((0, 7), (0, 13))
            await pilot.press("ctrl+c")
            assert copied[-1] == "chosen"

            application.show_dataframe(pd.DataFrame({"header_name": [1]}))
            table = application.query_one(ResultTable)
            table.select_header("header_name", 0)
            table.focus()
            await pilot.press("ctrl+c")
            assert copied[-1] == "header_name"

            monkeypatch.setattr(table, "copy_text", lambda: "")
            application.action_copy_focused()
            assert copied[-1] == "header_name"

    asyncio.run(exercise())
