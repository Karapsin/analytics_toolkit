from __future__ import annotations

import asyncio

import pytest
from analytics_toolkit.sql_explorer.file_commands import NewFileScreen, NewSqlFileScreen
from textual.app import App
from textual.widgets import Input, Static


def _assert_centered(screen: NewFileScreen) -> None:
    viewport = screen.content_region
    dialog = screen.query_one("#new-file-dialog").region
    left, right = dialog.x - viewport.x, viewport.right - dialog.right
    top, bottom = dialog.y - viewport.y, viewport.bottom - dialog.bottom
    assert screen.styles.align_horizontal == "center"
    assert screen.styles.align_vertical == "middle"
    assert dialog.width > 0
    assert dialog.height > 0
    assert min(left, right, top, bottom) >= 0
    assert abs(left - right) <= 1
    assert abs(top - bottom) <= 1


@pytest.mark.parametrize("kind", ["base", "sql"])
@pytest.mark.parametrize("action", ["confirm", "cancel"])
def test_filename_dialog_stays_centered_on_resize_without_losing_focus_or_validation(
    kind: str, action: str
) -> None:
    async def exercise() -> None:
        app = App[None]()
        async with app.run_test(size=(208, 47)) as pilot:
            screen = (
                NewSqlFileScreen()
                if kind == "sql"
                else NewFileScreen(suffix=".sql", title="Save SQL", placeholder="query.sql")
            )
            results: list[str | None] = []
            app.push_screen(screen, results.append)
            await pilot.pause()
            field = screen.query_one("#new-file-name", Input)
            for width, height in ((208, 47), (100, 40), (80, 30)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                _assert_centered(screen)
                assert app.screen is screen
                assert app.focused is field
                assert field.value == ""
                assert not results

            field.value = "nested/query.sql"
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen is screen
            assert app.focused is field
            assert not results
            assert str(screen.query_one("#new-file-notice", Static).renderable) == (
                "Enter one filename ending in .sql."
            )
            field.value = "report.sql"
            await pilot.press("enter" if action == "confirm" else "escape")
            await pilot.pause()
            assert results == (["report.sql"] if action == "confirm" else [None])
            assert app.screen is not screen

    asyncio.run(exercise())
