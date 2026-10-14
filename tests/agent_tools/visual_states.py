from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit.sql_explorer.create_table import creation_options
from analytics_toolkit.sql_explorer.create_table_screen import CreateTableScreen
from analytics_toolkit.sql_explorer.tabs import SaveChangesScreen
from analytics_toolkit.sql_explorer.widgets import ConfirmMutationScreen
from textual.widgets import Button, Static

from agent_tools import sql_explorer_visual, sql_explorer_visual_scene
from tests._support.paths import REPO_ROOT
from tests.agent_tools._support import wait_for_scene

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("scene", ["save-changes", "save-changes-cancel"])
def test_save_fixture_names_active_document_and_waits_for_requested_focus(
    scene: str,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        app = sql_explorer_visual_scene.VisualExplorerApp(
            scene, tmp_path / "evidence.json", REPO_ROOT / sql_explorer_visual.MANIFEST
        )
        async with app.run_test(size=(208, 47)) as pilot:
            await wait_for_scene(app, pilot)
            assert isinstance(app.screen, SaveChangesScreen)
            assert app.screen.title == app.active_workspace.file_label
            choice = "cancel" if scene.endswith("cancel") else "save"
            assert app.focused is app.screen.query_one(f"#save-changes-{choice}", Button)
            assert app._scene_state_ready()
            other = "save" if choice == "cancel" else "cancel"
            app.screen.query_one(f"#save-changes-{other}", Button).focus()
            await pilot.pause()
            assert not app._scene_state_ready()

    asyncio.run(exercise())


def test_invalid_fixture_uses_real_incomplete_column_validation(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = sql_explorer_visual_scene.VisualExplorerApp(
            "create-table-invalid",
            tmp_path / "evidence.json",
            REPO_ROOT / sql_explorer_visual.MANIFEST,
        )
        async with app.run_test(size=(208, 47)) as pilot:
            await wait_for_scene(app, pilot)
            screen = app.screen
            assert isinstance(screen, CreateTableScreen)
            with pytest.raises(ValueError, match="Each column needs"):
                creation_options(screen.values(), screen.backend)
            assert "Each column needs" in str(
                screen.query_one("#create-table-notice", Static).renderable
            )
            assert app._scene_state_ready()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "scene", ["mutation-confirm", "result-success", "results-vertical", "results-resized"]
)
def test_query_and_result_fixtures_describe_the_same_operation(scene: str, tmp_path: Path) -> None:
    async def exercise() -> None:
        app = sql_explorer_visual_scene.VisualExplorerApp(
            scene, tmp_path / "evidence.json", REPO_ROOT / sql_explorer_visual.MANIFEST
        )
        async with app.run_test(size=(208, 47)) as pilot:
            await pilot.pause()
            workspace = app.active_workspace
            if scene == "mutation-confirm":
                assert isinstance(app.screen, ConfirmMutationScreen)
                assert app.screen.plan.user_sql == workspace.editor.text
            else:
                table = workspace.result_table
                columns = [str(column.label) for column in table.ordered_columns]
                assert all(column in workspace.editor.text.split("FROM")[0] for column in columns)
                assert columns == (
                    ["order_id", "customer_name", "revenue", "long_note"]
                    if scene == "result-success"
                    else ["order_id", "customer_name", "revenue"]
                )
                if scene == "result-success":
                    result = workspace.last_run_result
                    assert table.row_count == result.displayed_rows == len(result.dataframe) == 20
                    assert result.total_rows == 1_248
                    assert result.truncated

    asyncio.run(exercise())
