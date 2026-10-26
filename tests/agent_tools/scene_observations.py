from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from queue import Queue
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agent_tools import sql_explorer_render_diagnostics as diagnostics
from agent_tools import sql_explorer_visual
from agent_tools import sql_explorer_visual_scene as scenes
from tests._support.paths import REPO_ROOT
from tests.agent_tools._support import wait_for_scene

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("scene", ["editor-command-caret", "new-file"])
def test_added_scene_diagnostics_observe_actual_layers_without_changing_state(
    tmp_path: Path, scene: str
) -> None:
    async def exercise() -> None:
        app = scenes.VisualExplorerApp(
            scene, tmp_path / "geometry.json", REPO_ROOT / sql_explorer_visual.MANIFEST
        )
        async with app.run_test(size=(208, 47)) as pilot:
            await wait_for_scene(app, pilot)
            editor = app.active_workspace.editor
            screen = app.screen
            before = (app.focused, editor.text, editor.cursor_selections, editor.scroll_offset)
            result = diagnostics.terminal_diagnostics(app, scene)
            assert before == (
                app.focused,
                editor.text,
                editor.cursor_selections,
                editor.scroll_offset,
            )
            assert scene in diagnostics.SCENES
            assert len(result["regions"]) <= diagnostics.MAX_REGIONS
            assert sum(len(row["widget_rows"]) for row in result["regions"]) <= diagnostics.MAX_ROWS
            if scene == "editor-command-caret":
                snapshot = result["multicursor"]
                assert snapshot["cursor_count"] == 1
                assert snapshot["primary_identity"] == id(editor.selection)
                assert snapshot["focused_widget"]["identity"] == id(app.focused)
                assert snapshot["expected"]["focused_widget_id"] == "command-input"
                caret = snapshot["carets"][0]
                assert caret["start"] == list(editor.selection.start)
                assert caret["end"] == list(editor.selection.end) == [1, 14]
                assert caret["primary"]
                assert caret["screen_cell"] == [
                    editor.content_region.x + editor.gutter_width + 14,
                    editor.content_region.y + 1,
                ]
                for layer in ("widget_rows", "composed_rows"):
                    assert caret[layer] is not None
                    assert caret[layer]["text"] == editor.document[1][14]
                    assert caret[layer]["cells"] == 1
            else:
                snapshot = result["new_file"]
                assert snapshot["screen"]["class"] == type(screen).__name__
                assert snapshot["screen"]["mounted"]
                assert snapshot["screen_mro"][0].endswith(".NewSqlFileScreen")
                dialog = screen.query_one("#new-file-dialog")
                assert snapshot["dialog"]["region"] == diagnostics._region_record(dialog.region)
                assert snapshot["dialog_geometry"]["ancestors"]
                assert snapshot["screen"]["styles"]["align_horizontal"] == str(
                    screen.styles.align_horizontal
                )
                assert snapshot["screen"]["styles"]["align_vertical"] == str(
                    screen.styles.align_vertical
                )
                assert all(entry["widget_rows"] for entry in result["regions"])
                assert all(entry["composed_rows"] for entry in result["regions"])
                geometry = json.loads((tmp_path / "geometry.json").read_text(encoding="utf-8"))
                for name in (
                    "new_file_center_middle",
                    "new_file_dialog_contained",
                    "new_file_horizontally_centered",
                    "new_file_vertically_centered",
                ):
                    assert geometry["assertions"][name] is True

    asyncio.run(exercise())


def _layout_widget() -> SimpleNamespace:
    region = SimpleNamespace(x=2, y=3, width=80, height=10)
    return SimpleNamespace(
        id="observed",
        is_mounted=True,
        region=region,
        content_region=region,
        scrollable_content_region=region,
        scroll_offset=(4, 5),
        styles=SimpleNamespace(align_horizontal="left"),
        _layout_required=True,
        _message_queue=Queue(),
    )


def test_ruler_lifecycle_preserves_queues_and_reports_unavailable_probes() -> None:
    async def exercise() -> None:
        widget = _layout_widget()
        widget._message_queue.put_nowait("pending layout")
        app = SimpleNamespace(
            focused=widget,
            active_workspace=SimpleNamespace(
                editor=widget, command_input=widget, query_one=lambda _query: widget
            ),
        )
        rows: dict[str, Any] = {
            "ruler": {
                "widget_row": {"text": "1", "cells": 1},
                "compositor_row": {"text": "1   ", "cells": 4},
            }
        }
        original = deepcopy(rows)
        snapshot = diagnostics.ruler_lifecycle_snapshot(app, rows)
        assert rows == original
        assert snapshot["focused_widget"]["identity"] == id(widget)
        assert snapshot["row_lengths"]["ruler"]["widget_row"]["cells"] == 1
        assert snapshot["row_lengths"]["ruler"]["compositor_row"]["cells"] == 4
        for state in snapshot["widgets"].values():
            assert state["pending"]["_layout_required"] is True
            assert state["pending"]["_repaint_required"] == {"unavailable": "AttributeError"}
            assert state["queued_messages"] == 1
            assert state["scroll_offset"] == [4, 5]
            assert state["content_region"]["width"] == 80
        assert widget._message_queue.qsize() == 1
        assert widget._message_queue.get_nowait() == "pending layout"
        assert widget._layout_required is True
        assert len(json.dumps(snapshot).encode("utf-8")) <= diagnostics.MAX_LIFECYCLE_BYTES

    asyncio.run(exercise())


def test_ruler_snapshot_byte_limit_explicitly_reports_incompleteness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = _layout_widget()
    app = SimpleNamespace(
        focused=widget,
        active_workspace=SimpleNamespace(
            editor=widget, command_input=widget, query_one=lambda _query: widget
        ),
    )
    monkeypatch.setattr(diagnostics, "MAX_LIFECYCLE_BYTES", 128)
    assert diagnostics.ruler_lifecycle_snapshot(app, {}) == {
        "complete": False,
        "omissions": ["lifecycle_byte_limit"],
    }


def test_new_file_missing_screen_context_is_not_an_alignment_verdict() -> None:
    result: dict[str, Any] = {"missing": [], "omissions": []}
    diagnostics._new_file_diagnostics(SimpleNamespace(), result)
    assert result["missing"] == ["new-file:state:AttributeError"]
    assert "new_file" not in result


def test_layout_probe_truncation_and_missing_queue_are_explicit() -> None:
    widget = _layout_widget()
    del widget._message_queue
    widget.styles.align_horizontal = "x" * (diagnostics.MAX_CELLS + 1)
    snapshot = diagnostics._layout_observation(widget)
    assert snapshot["queued_messages"] == {"unavailable": "AttributeError"}
    assert snapshot["styles"]["align_horizontal"] == {
        "value": "x" * diagnostics.MAX_CELLS,
        "truncated": True,
    }
    assert snapshot["styles"]["align_vertical"] == {"unavailable": "AttributeError"}
