from __future__ import annotations

import asyncio

from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.inputs import EditableInput
from analytics_toolkit.sql_explorer.widgets import FileNavigationScreen

from tests.sql.explorer.app import FakeSession


def test_directory_picker_escape_arms_enter_and_second_escape_cancels(tmp_path) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            selected: list[object] = []
            picker = FileNavigationScreen(tmp_path, select_directory=True)
            await application.push_screen(picker, selected.append)

            await pilot.press("escape")
            assert picker.is_active
            assert picker._directory_confirmation_armed is True
            assert application.focused is picker.query_one("#navigation-select-directory")

            await pilot.press("enter")
            await pilot.pause()
            assert selected == [tmp_path.resolve()]

            cancelled: list[object] = []
            picker = FileNavigationScreen(tmp_path, select_directory=True)
            await application.push_screen(picker, cancelled.append)
            await pilot.press("escape", "escape")
            await pilot.pause()
            assert cancelled == [None]

    asyncio.run(exercise())


def test_directory_confirmation_arrows_resume_and_apply_navigation(tmp_path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alps").mkdir()

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            for key in (
                "left",
                "right",
                "ctrl+left",
                "ctrl+right",
                "shift+left",
                "up",
                "down",
            ):
                picker = FileNavigationScreen(tmp_path, select_directory=True)
                await application.push_screen(picker)
                path_input = picker.query_one("#navigation-path", EditableInput)
                path_input.value = "a"
                base_key = key.rsplit("+", 1)[-1]
                path_input.cursor_position = 1 if base_key == "left" else 0
                await pilot.pause()

                await pilot.press("escape", key)
                assert picker.is_active
                assert picker._directory_confirmation_armed is False
                assert application.focused is path_input
                if base_key == "left":
                    assert path_input.cursor_position == 0
                    if key == "shift+left":
                        assert path_input.selected_text == "a"
                elif base_key == "right":
                    assert path_input.cursor_position == 1
                else:
                    assert picker._match_index != -1

                picker.dismiss(None)
                await pilot.pause()

    asyncio.run(exercise())


def test_open_file_picker_still_cancels_on_first_escape(tmp_path) -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            dismissed: list[object] = []
            picker = FileNavigationScreen(tmp_path)
            await application.push_screen(picker, dismissed.append)
            await pilot.press("escape")
            await pilot.pause()
            assert dismissed == [None]

    asyncio.run(exercise())
