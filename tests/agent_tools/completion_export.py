from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from textual.document._document import Selection
from textual.screen import Screen

from agent_tools import sql_explorer_export_observer as observer
from agent_tools import sql_explorer_render_diagnostics as diagnostics
from agent_tools import sql_explorer_svg_whitespace as whitespace
from agent_tools import sql_explorer_visual, sql_explorer_visual_scene
from tests._support.paths import REPO_ROOT
from tests.agent_tools._support import wait_for_scene
from tests.sql._support.explorer import FakeSession

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("scene", ["completion-empty-columns", "completion-narrowed"])
def test_actual_completion_export_keeps_same_invocation_space_and_popup_evidence(
    tmp_path: Path, scene: str
) -> None:
    async def exercise() -> None:
        app = sql_explorer_visual_scene.create_scene(
            scene, tmp_path / "geometry.json", REPO_ROOT / sql_explorer_visual.MANIFEST
        )
        async with app.run_test(size=(208, 47)) as pilot:
            await wait_for_scene(app, pilot)
            editor = app.active_workspace.editor
            before = (
                app.focused,
                editor.text,
                tuple(editor.cursor_selections),
                dict(editor._cursor_preferences),
                editor.scroll_offset,
                app.active_workspace.completion_menu.region,
            )
            terminal = diagnostics.terminal_diagnostics(app, scene)
            carets = terminal["multicursor"]["carets"]
            assert carets
            assert all(caret["in_document"] for caret in carets)
            for caret in carets:
                for layer in ("widget_rows", "composed_rows"):
                    row = caret[layer]
                    assert row["text"].isspace()
                    assert all(span["flags"]["underline"] for span in row["spans"])
            saved = deepcopy(terminal)
            with observer.observe_export() as result:
                svg = app.export_screenshot(title=scene)
            observer.map_export_spans(result, svg)
            repaired, restored = whitespace.restore_omitted_underlines(svg, result)
            assert restored
            assert "data-terminal-whitespace" in repaired
            assert {tuple(record["export_cell"]) for record in restored} >= {
                tuple(diagnostics._caret_mapping(editor, selection)["screen_cell"])
                for selection in editor.cursor_selections
            }
            if scene == "completion-narrowed":
                assert app.active_workspace.completion_menu.suggestions == ("customer_name",)
            assert len(result["invocations"]) == 1
            recording = result["invocations"][0]["recording"]
            assert any(
                record["text"].isspace() and record["style"]["flags"]["underline"]
                for record in recording["segments"]
            )
            assert terminal == saved
            assert before == (
                app.focused,
                editor.text,
                tuple(editor.cursor_selections),
                dict(editor._cursor_preferences),
                editor.scroll_offset,
                app.active_workspace.completion_menu.region,
            )
            assert app.active_workspace.completion_menu.is_open
            # The untouched export remains parseable, including the completion suffix.
            texts = "".join(ET.fromstring(svg).itertext())
            assert "FROM" in texts

    asyncio.run(exercise())


@pytest.mark.parametrize("command_focus", [False, True])
@pytest.mark.parametrize("character", [" ", "\t"])
@pytest.mark.parametrize("scrolled", [False, True])
def test_primary_secondary_whitespace_export_observation_preserves_composed_cells(
    command_focus: bool, character: str, scrolled: bool
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(100, 30)) as pilot:
            editor = app.active_workspace.editor
            prefix = "x" * 120 + "SELECT" if scrolled else "SELECT"
            column = len(prefix)
            editor.text = f"{prefix}{character}FROM orders\n{prefix}{character}FROM customers"
            editor._set_selections(Selection.cursor((0, column)), [Selection.cursor((1, column))])
            if command_focus:
                app.active_workspace.command_input.focus()
            else:
                editor.focus()
            await pilot.pause()
            before = (
                app.focused,
                editor.text,
                tuple(editor.cursor_selections),
                dict(editor._cursor_preferences),
                editor.scroll_offset,
            )
            composed = app.screen._compositor.render_strips()
            expected_rows = [list(row) for row in composed]
            for selection in editor.cursor_selections:
                mapping = diagnostics._caret_mapping(editor, selection)
                x, y = mapping["screen_cell"]
                cells = composed[y].crop(x, x + mapping["cell_width"])
                assert cells.text.isspace()
                assert all(segment.style.underline for segment in cells)
            with observer.observe_export() as result:
                svg = app.export_screenshot(title="whitespace")
            repaired, restored = whitespace.restore_omitted_underlines(svg, result)
            assert repaired != svg
            assert {tuple(record["export_cell"]) for record in restored} >= {
                tuple(diagnostics._caret_mapping(editor, selection)["screen_cell"])
                for selection in editor.cursor_selections
            }
            assert len(result["invocations"]) == 1
            records = result["invocations"][0]["recording"]["segments"]
            assert any(
                record["style"]["flags"]["underline"] and record["text"].isspace()
                for record in records
            )
            assert [list(row) for row in app.screen._compositor.render_strips()] == expected_rows
            assert before == (
                app.focused,
                editor.text,
                tuple(editor.cursor_selections),
                dict(editor._cursor_preferences),
                editor.scroll_offset,
            )

    asyncio.run(exercise())


def test_occluded_editor_is_not_synthesized_into_the_recorded_screen() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(100, 30)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "SELECT FROM orders"
            editor._set_selections(Selection.cursor((0, 6)), [])
            await pilot.pause()
            editor_state = (
                editor.text,
                tuple(editor.cursor_selections),
                dict(editor._cursor_preferences),
                editor.scroll_offset,
            )
            overlay = Screen()
            overlay.styles.background = "#000000"
            app.push_screen(overlay)
            await pilot.pause()
            focused = app.focused
            with observer.observe_export() as result:
                svg = app.export_screenshot(title="occluded editor")
            repaired, restored = whitespace.restore_omitted_underlines(svg, result)
            assert repaired == svg
            assert restored == []
            assert app.screen is overlay
            assert app.focused is focused
            assert "SELECT" not in "".join(ET.fromstring(svg).itertext())
            recording = result["invocations"][0]["recording"]
            assert not any("SELECT" in record["text"] for record in recording["segments"])
            assert editor_state == (
                editor.text,
                tuple(editor.cursor_selections),
                dict(editor._cursor_preferences),
                editor.scroll_offset,
            )

    asyncio.run(exercise())
