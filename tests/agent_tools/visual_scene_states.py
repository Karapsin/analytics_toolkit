from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.geometry import Region
from textual.widgets import Button, Input

from agent_tools import mcp_server, sql_explorer_visual, sql_explorer_visual_scene


@pytest.mark.parametrize(
    ("scene", "guard"),
    [
        ("create-table-invalid", "visible_invalid_draft"),
        ("create-table-types", "types_anchor_to_active_field"),
        ("mutation-confirm", "mutation_matches_editor"),
        ("result-success", "result_rows_match_status"),
        ("results-resized", "result_columns_match_query"),
        ("save-changes", "save_targets_active_document"),
        ("save-changes-cancel", "expected_save_button_focused"),
    ],
)
def test_scene_guards_reject_inconsistent_final_state(
    scene: str, guard: str, tmp_path: Path
) -> None:
    async def exercise() -> None:
        app = sql_explorer_visual_scene.VisualExplorerApp(
            scene,
            tmp_path / "geometry.json",
            mcp_server.REPO_ROOT / sql_explorer_visual.MANIFEST,
        )
        async with app.run_test(size=(208, 47)) as pilot:
            for _attempt in range(30):
                await pilot.pause(0.1)
                if app.creation_scene_ready:
                    break
            await pilot.pause()
            assert sql_explorer_visual_scene._scene_state_assertions(app, scene)[guard]
            if scene == "create-table-invalid":
                list(app.screen.query(".create-column-type"))[-1].value = "NUMERIC(18,2)"
            elif scene == "create-table-types":
                app.screen.query_one("#create-table-name", Input).focus()
            elif scene == "mutation-confirm":
                app.active_workspace.editor.text = "SELECT 1"
            elif scene == "result-success":
                app.active_workspace.result_table.clear()
            elif scene == "results-resized":
                app.active_workspace.editor.text = (
                    "SELECT wrong_column FROM analytics.daily_orders;"
                )
            elif scene == "save-changes":
                app.active_workspace.current_file = Path("different.sql")
            else:
                app.screen.query_one("#save-changes-save", Button).focus()
            await pilot.pause()
            assert not sql_explorer_visual_scene._scene_state_assertions(app, scene)[guard]

    asyncio.run(exercise())


def test_completion_geometry_guard_rejects_overlapping_popup(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = sql_explorer_visual_scene.VisualExplorerApp(
            "completion-columns",
            tmp_path / "geometry.json",
            mcp_server.REPO_ROOT / sql_explorer_visual.MANIFEST,
        )
        async with app.run_test(size=(208, 47)) as pilot:
            await pilot.pause()
            workspace = app.active_workspace
            menu = workspace.completion_menu
            app._position_completion(workspace)
            await pilot.pause()
            guards = sql_explorer_visual_scene._completion_geometry_assertions(workspace)
            assert all(guards.values())
            # Freeze normal repair so the deliberately broken geometry persists.
            app._schedule_completion_position = lambda _workspace: None
            row = (
                workspace.editor.scrollable_content_region.y
                + workspace.editor.cursor_render_offset[1]
                - int(workspace.editor.scroll_y)
            )
            origin = workspace.query_one(".query-pane").content_region
            menu.styles.offset = (menu.region.x - origin.x, row - origin.y)
            await pilot.pause()
            guards = sql_explorer_visual_scene._completion_geometry_assertions(workspace)
            assert not guards["completion_active_row_visible"]
            assert guards["completion_contained"]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("row", "region", "visible", "contained"),
    [
        (0, Region(1, 3, 32, 3), True, True),
        (-1, Region(1, 3, 32, 3), False, True),
        (0, Region(1, 2, 32, 3), False, True),
        (0, Region(-1, 3, 32, 3), True, False),
        (0, Region(0, 3, 32, 3), True, False),
        (0, Region(48, 3, 32, 3), True, False),
        (0, Region(60, 3, 32, 3), True, False),
        (0, Region(1, 17, 32, 3), True, False),
    ],
)
def test_completion_geometry_guard_checks_row_visibility_and_containment(
    row: int, region: Region, visible: bool, contained: bool
) -> None:
    workspace = SimpleNamespace(
        editor=SimpleNamespace(
            scrollable_content_region=Region(1, 2, 78, 16),
            cursor_render_offset=(10, row),
            scroll_y=0,
        ),
        completion_menu=SimpleNamespace(
            is_open=True,
            region=region,
            parent=SimpleNamespace(content_region=Region(0, 0, 80, 20)),
        ),
    )
    guards = sql_explorer_visual_scene._completion_geometry_assertions(workspace)
    assert guards["completion_active_row_visible"] is visible
    assert guards["completion_contained"] is contained


def test_invalid_scene_guard_requires_disabled_create(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = sql_explorer_visual_scene.VisualExplorerApp(
            "create-table-invalid",
            tmp_path / "geometry.json",
            mcp_server.REPO_ROOT / sql_explorer_visual.MANIFEST,
        )
        async with app.run_test(size=(208, 47)) as pilot:
            for _attempt in range(30):
                await pilot.pause(0.1)
                if app.creation_scene_ready:
                    break
            await pilot.pause()
            assert sql_explorer_visual_scene._scene_state_assertions(app, "create-table-invalid")[
                "invalid_create_disabled"
            ]
            app.screen.query_one("#create-table-submit", Button).disabled = False
            assert not sql_explorer_visual_scene._scene_state_assertions(
                app, "create-table-invalid"
            )["invalid_create_disabled"]

    asyncio.run(exercise())
