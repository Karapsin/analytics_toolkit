from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from functools import wraps
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from analytics_toolkit.sql_explorer.create_table import creation_options
from analytics_toolkit.sql_explorer.create_table_screen import CreateTableScreen
from analytics_toolkit.sql_explorer.tabs import SaveChangesScreen
from analytics_toolkit.sql_explorer.widgets import ConfirmMutationScreen, FileNavigationScreen
from rich.text import Text
from textual.css.query import NoMatches
from textual.widgets import Button, Input, Static

from agent_tools import sql_explorer_render_diagnostics as diagnostics
from agent_tools import sql_explorer_visual, sql_explorer_visual_capture
from agent_tools import sql_explorer_visual_scene as scenes
from tests._support.paths import REPO_ROOT
from tests.agent_tools._support import wait_for_scene

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any


@pytest.mark.parametrize("scene", ["save-changes", "save-changes-cancel"])
def test_save_fixture_names_active_document_and_waits_for_requested_focus(
    scene: str,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        app = scenes.VisualExplorerApp(
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
        app = scenes.VisualExplorerApp(
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
        app = scenes.VisualExplorerApp(
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


def test_tab_database_ruler_evidence_observes_separate_widget_and_compositor_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        evidence_path = tmp_path / "evidence.json"
        manifest = REPO_ROOT / sql_explorer_visual.MANIFEST
        app = scenes.VisualExplorerApp("tab-database-change", evidence_path, manifest)
        switched = asyncio.Event()
        switch_database = app._switch_visual_database

        @wraps(switch_database)
        def observe_switch() -> None:
            switch_database()
            switched.set()

        monkeypatch.setattr(app, "_switch_visual_database", observe_switch)
        async with app.run_test(size=(208, 47)) as pilot:
            await asyncio.wait_for(switched.wait(), timeout=5)
            await pilot.pause()
            workspace = app.active_workspace
            editor = workspace.editor
            original = (
                app.focused,
                editor.text,
                editor.cursor_selections,
                editor.scroll_offset,
            )
            scenes._write_evidence(app, "tab-database-change", evidence_path, manifest)
            geometry = json.loads(evidence_path.read_text(encoding="utf-8"))
            snapshot = geometry["ruler_evidence"]
            observation = diagnostics.ruler_lifecycle_snapshot(app, snapshot)
            try:
                assert geometry["ok"]
                assert snapshot["active_workspace"] == workspace.tab_id == "2"
                assert snapshot["cursor_location"] == list(editor.cursor_location) == [0, 0]
                assert snapshot["active_column"] == 1
                assert snapshot["document_empty"]
                assert snapshot["cursor_render_offset"] == list(editor.cursor_render_offset)
                assert snapshot["scroll_x"] == int(editor.scroll_x) == 0
                assert snapshot["scroll_y"] == int(editor.scroll_y) == 0
                assert snapshot["gutter_width"] == editor.gutter_width
                assert snapshot["gutter_left"] == editor.gutter.left
                assert snapshot["active_column_screen_x"] == (
                    editor.content_region.x + editor.gutter_width
                )
                assert snapshot["ruler"]["region"]["bottom"] == snapshot["editor"]["region"]["y"]
                assert snapshot["ruler"]["region"]["height"] == 1
                _assert_ruler_rows(app, snapshot)
                assert snapshot["ruler"]["widget_row"]["text"].strip() == "1"
                assert (
                    snapshot["editor"]["widget_row"]["text"][: editor.gutter_width].strip() == "1"
                )
                assert (
                    snapshot["editor"]["widget_row"]["text"][editor.gutter_width :].strip() == "│"
                )
                assert app.focused is workspace.command_input
                assert original == (
                    app.focused,
                    editor.text,
                    editor.cursor_selections,
                    editor.scroll_offset,
                )
                assert all(scenes._ruler_evidence_assertions(snapshot).values())
            except AssertionError as error:
                failure = {"lifecycle": observation, "ruler_evidence": snapshot}
                raise AssertionError(json.dumps(failure, sort_keys=True)) from error

            _assert_ruler_negative_controls(snapshot)

            # A final export owns the observed rows just as it owns other diagnostics.
            geometry["render_diagnostics"] = {"frame": "owned"}
            evidence_path.write_text(json.dumps(geometry), encoding="utf-8")
            owned = evidence_path.read_bytes()
            scenes._write_evidence(app, "tab-database-change", evidence_path, manifest)
            assert evidence_path.read_bytes() == owned

    asyncio.run(exercise())


def _assert_ruler_rows(app: scenes.VisualExplorerApp, snapshot: dict[str, Any]) -> None:
    workspace = app.active_workspace
    ruler = workspace.query_one("#column-ruler", Static)
    composed = app.screen._compositor.render_strips()
    for name, widget in (("ruler", ruler), ("editor", workspace.editor)):
        region = widget.content_region
        entry = snapshot[name]
        assert entry["region"] == scenes._widget_geometry(widget)
        for layer in ("widget_row", "compositor_row"):
            row = entry[layer]
            assert row["x"] == region.x
            assert row["y"] == region.y
            assert row["cells"] <= 208
            assert not row["truncated"]
        assert entry["widget_row"]["text"] == widget.render_line(0).crop(0, region.width).text
        assert entry["compositor_row"]["text"] == (
            composed[region.y].crop(region.x, region.right).text
        )
        assert entry["widget_row"]["text"] == entry["compositor_row"]["text"]


def _assert_ruler_negative_controls(snapshot: dict[str, Any]) -> None:
    overlap = deepcopy(snapshot)
    overlap["ruler"]["region"]["bottom"] += 1
    assert not scenes._ruler_evidence_assertions(overlap)["ruler:separate_rows"]
    shifted = deepcopy(snapshot)
    shifted["active_column_screen_x"] += 1
    assert not scenes._ruler_evidence_assertions(shifted)["ruler:active_column_cell"]
    lost_label = deepcopy(snapshot)
    lost_label["ruler"]["compositor_row"]["text"] = " " * snapshot["ruler"]["widget_row"]["cells"]
    assert not scenes._ruler_evidence_assertions(lost_label)["ruler:active_column_cell"]
    lost_gutter = deepcopy(snapshot)
    lost_gutter["editor"]["compositor_row"]["text"] = (
        " " * snapshot["editor"]["widget_row"]["cells"]
    )
    assert not scenes._ruler_evidence_assertions(lost_gutter)["ruler:first_gutter_separate"]


def _armed_app() -> tuple[SimpleNamespace, Mock, dict[str, Mock]]:
    screen = Mock(spec=FileNavigationScreen)
    screen.is_mounted = True
    screen.select_directory = True
    screen._directory_confirmation_armed = True
    controls = {
        selector: Mock(is_mounted=True)
        for selector in (
            "#navigation-path",
            "#navigation-tree",
            "#navigation-select-directory",
            "#navigation-notice",
        )
    }
    button = controls["#navigation-select-directory"]
    button.has_class.return_value = True
    controls["#navigation-notice"].renderable = Text(scenes.DIRECTORY_ARMED_NOTICE)
    screen.query_one.side_effect = lambda selector, *_args: controls[selector]
    app = SimpleNamespace(
        visual_scene_id="directory-armed",
        scene_configured=True,
        screen_stack=[screen],
        screen=screen,
        focused=button,
    )
    app._scene_readiness_details = lambda: scenes.VisualExplorerApp._scene_readiness_details(app)
    return app, screen, controls


@pytest.mark.parametrize(
    "failure",
    [
        "none",
        "empty_stack",
        "wrong_screen",
        "unmounted_screen",
        "file_mode",
        "unarmed",
        "button_class",
        "focus",
        "notice",
    ],
)
def test_directory_readiness_requires_every_armed_invariant(failure: str) -> None:
    app, screen, controls = _armed_app()
    if failure == "empty_stack":
        app.screen_stack = []
    elif failure == "wrong_screen":
        app.screen = SimpleNamespace(is_mounted=True)
    elif failure == "unmounted_screen":
        screen.is_mounted = False
    elif failure == "file_mode":
        screen.select_directory = False
    elif failure == "unarmed":
        screen._directory_confirmation_armed = False
    elif failure == "button_class":
        controls["#navigation-select-directory"].has_class.return_value = False
    elif failure == "focus":
        app.focused = controls["#navigation-path"]
    elif failure == "notice":
        controls["#navigation-notice"].renderable = Text("21 matching entries.")
    details = scenes.VisualExplorerApp.scene_readiness_details(app)
    assert details == app._scene_readiness_details()
    assert all(details["conditions"].values()) == (failure == "none")
    assert scenes.VisualExplorerApp._scene_state_ready(app) == (failure == "none")
    screen.action_cancel.assert_not_called()


@pytest.mark.parametrize(
    "selector",
    [
        "#navigation-path",
        "#navigation-tree",
        "#navigation-select-directory",
        "#navigation-notice",
    ],
)
@pytest.mark.parametrize("failure", ["missing", "unmounted"])
def test_directory_readiness_rejects_each_unavailable_control(selector: str, failure: str) -> None:
    app, screen, controls = _armed_app()
    if failure == "missing":

        def lookup(requested: str, *_args: object) -> Mock:
            if requested == selector:
                message = "requested directory control absent"
                raise NoMatches(message)
            return controls[requested]

        screen.query_one.side_effect = lookup
    else:
        controls[selector].is_mounted = False
    details = scenes.VisualExplorerApp.scene_readiness_details(app)
    assert details == app._scene_readiness_details()
    key = "missing_controls" if failure == "missing" else "unmounted_controls"
    assert selector in details[key]
    assert not scenes.VisualExplorerApp._scene_state_ready(app)


def test_directory_fixture_stays_armed_and_records_semantic_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "queries").mkdir()

    async def exercise() -> None:
        evidence = tmp_path / "evidence.json"
        manifest = REPO_ROOT / sql_explorer_visual.MANIFEST
        app = scenes.VisualExplorerApp("directory-armed", evidence, manifest)
        async with app.run_test(size=(208, 47)) as pilot:
            await wait_for_scene(app, pilot)
            picker = app.screen
            assert isinstance(picker, FileNavigationScreen)
            assert picker._directory_confirmation_armed
            button = picker.query_one("#navigation-select-directory", Button)
            assert button.has_class("armed")
            assert app.focused is button
            assert str(picker.query_one("#navigation-notice", Static).renderable) == (
                scenes.DIRECTORY_ARMED_NOTICE
            )
            await pilot.pause()
            app._refresh_visual_evidence()
            await pilot.pause()
            assert app._scene_state_ready()
            geometry = json.loads(evidence.read_text(encoding="utf-8"))
            assert geometry["ok"]
            assert geometry["scene_readiness"]["notice_text"] == scenes.DIRECTORY_ARMED_NOTICE
            assert all(
                value
                for name, value in geometry["assertions"].items()
                if name.startswith("directory:")
            )
            details = sql_explorer_visual_capture._timeout_diagnostics(
                app, "directory-armed", evidence, manifest
            )
            assert all(details["conditions"].values())
            # Losing focus must fail final capture and leave useful fresh diagnostics.
            picker.query_one("#navigation-path", Input).focus()
            await pilot.pause()
            app._refresh_visual_evidence()
            assert not app.creation_scene_ready
            assert not button.has_focus
            with pytest.raises(RuntimeError, match="not ready for final capture"):
                sql_explorer_visual_capture._capture_frame(
                    app, "directory-armed", evidence, manifest, tmp_path / "unused.png"
                )
            details = sql_explorer_visual_capture._timeout_diagnostics(
                app, "directory-armed", evidence, manifest
            )
            assert not details["conditions"]["requested_focus"]
            assert "directory:requested_focus" in details["geometry"]["failed_assertions"]

    asyncio.run(exercise())


@pytest.mark.parametrize("key", ["enter", "escape", "up", "down", "left", "right"])
def test_armed_fixture_preserves_selection_cancel_and_browsing_actions(
    key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "queries").mkdir()

    async def exercise() -> None:
        app = scenes.VisualExplorerApp(
            "directory-armed",
            tmp_path / "evidence.json",
            REPO_ROOT / sql_explorer_visual.MANIFEST,
        )
        async with app.run_test(size=(208, 47)) as pilot:
            await wait_for_scene(app, pilot)
            picker = app.screen
            assert isinstance(picker, FileNavigationScreen)
            selected: list[Path | None] = []
            original = picker.dismiss

            def dismiss(result: Path | None = None) -> None:
                selected.append(result)
                original(result)

            monkeypatch.setattr(picker, "dismiss", dismiss)
            await pilot.press(key)
            await pilot.pause()
            if key in {"enter", "escape"}:
                assert selected == ([tmp_path] if key == "enter" else [None])
                assert app.screen is not picker
            else:
                assert app.screen is picker
                assert not picker._directory_confirmation_armed
                button = picker.query_one("#navigation-select-directory", Button)
                assert not button.has_class("armed")
                assert app.focused is picker.query_one("#navigation-path", Input)
                app._refresh_visual_evidence()
                assert not app.creation_scene_ready
                assert not picker._directory_confirmation_armed

    asyncio.run(exercise())
