from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from rich.segment import Segment
from rich.style import Style
from textual.geometry import Region
from textual.strip import Strip

from agent_tools import sql_explorer_render_diagnostics as diagnostics
from agent_tools import sql_explorer_visual, sql_explorer_visual_scene
from tests._support.paths import REPO_ROOT
from tests.agent_tools._support import wait_for_scene
from tests.agent_tools.styled_rows import StyledRowsApp

if TYPE_CHECKING:
    from pathlib import Path

    from textual.widgets import Static


def _collect(widget: Static, composed: list[Strip], *, budget: int = 24) -> dict[str, Any]:
    result: dict[str, Any] = {"regions": [], "missing": [], "omissions": []}
    remaining = diagnostics._collect_border_region(("probe", widget, 9), composed, result, budget)
    result["remaining"] = remaining
    return result


def test_outer_border_samples_preserve_native_coordinates_styles_and_raw_content() -> None:
    async def exercise() -> None:
        app = StyledRowsApp()
        async with app.run_test(size=(40, 15)) as pilot:
            await pilot.pause()
            widget = app.query_one("#probe")
            before = (app.focused, widget.region, widget.content_region, widget.scroll_offset)
            raw = widget.render_line(0)
            segments = list(raw)
            composed = app.screen._compositor.render_strips()
            result = _collect(widget, composed)
            entry = result["regions"][0]
            assert not result["missing"]
            assert entry["geometry"]["region"] == diagnostics._region_record(widget.region)
            assert entry["geometry"]["ancestors"]
            assert "solid" in entry["geometry"]["styles"]["border"]
            assert entry["raw_content_row"] == diagnostics.strip_record(
                raw,
                x=widget.content_region.x,
                y=widget.content_region.y,
                width=widget.content_region.width,
            )
            assert entry["raw_content_observation"]["segments"] == [
                {"text": segment.text, "style": str(segment.style), "control": str(segment.control)}
                for segment in segments
            ]
            assert entry["raw_content_row"]["text"].startswith("界e\u0301")
            assert entry["coverage"]["unsampled_edges"] == {
                "top": [],
                "bottom": [],
                "left": [[2, 3], [4, 5]],
                "right": [[2, 3], [4, 5]],
            }
            assert result["omissions"] == ["probe:unsampled_edges"]
            assert result["remaining"] == 15
            assert len(entry["border_samples"]) == 8
            for sample in entry["border_samples"]:
                local = sample["widget_local_clip"]
                screen = sample["screen_clip"]
                assert screen["x"] == widget.region.x + local["x"]
                assert screen["y"] == widget.region.y + local["y"]
                assert screen["width"] == local["width"]
                expected = diagnostics.strip_record(
                    composed[screen["y"]].crop(screen["x"], screen["x"] + screen["width"]),
                    x=screen["x"],
                    y=screen["y"],
                    width=screen["width"],
                )
                assert sample["compositor_row"] == expected
                assert sample["layers_equal"]
                assert not sample["missing"]
            assert list(raw) == segments
            assert list(widget.render_line(0)) == segments
            assert before == (
                app.focused,
                widget.region,
                widget.content_region,
                widget.scroll_offset,
            )

    asyncio.run(exercise())


def _sample_widget() -> SimpleNamespace:
    strip = Strip([Segment("│界e\u0301 │", Style(color="#abcdef", bgcolor="#123456"))])
    return SimpleNamespace(
        region=Region(3, 2, strip.cell_length, 1),
        render_lines=lambda clip: [strip.crop(clip.x, clip.right)],
    )


@pytest.mark.parametrize("damage", ["shift", "missing", "short", "style"])
def test_border_layer_mismatch_retains_actual_bytes_without_repair(damage: str) -> None:
    widget = _sample_widget()
    clip = Region(0, 0, widget.region.width, 1)
    original = widget.render_lines(clip)[0]
    screen = Strip([Segment(" " * 3), *original, Segment("  ")])
    composed = [Strip.blank(20), Strip.blank(20), screen]
    if damage == "missing":
        composed.pop()
    elif damage == "short":
        composed[2] = screen.crop(0, 4)
    elif damage == "style":
        composed[2] = Strip([Segment(screen.text, Style(color="#ff0000"))])
    else:
        composed[2] = Strip([Segment(" "), *screen])
    before = [list(strip) for strip in composed]
    sample = diagnostics._border_sample(widget, composed, clip)
    assert not sample["layers_equal"]
    assert sample["styled_widget_row"]["text"] == original.text
    assert sample["styled_widget_row"]["cells"] == original.cell_length
    assert [list(strip) for strip in composed] == before
    assert widget.render_lines(clip)[0].text == original.text
    if damage in {"missing", "short"}:
        assert sample["missing"]
    else:
        assert not sample["missing"]


def test_unicode_border_sample_retains_combining_marks_wide_cells_and_styles() -> None:
    widget = _sample_widget()
    clip = Region(0, 0, widget.region.width, 1)
    strip = widget.render_lines(clip)[0]
    composed = [Strip.blank(20), Strip.blank(20), Strip([Segment("   "), *strip])]
    sample = diagnostics._border_sample(widget, composed, clip)
    assert sample["layers_equal"]
    assert not sample["missing"]
    assert sample["styled_widget_row"]["text"] == "│界e\u0301 │"
    assert sample["styled_widget_row"]["cells"] == 6
    assert sample["styled_widget_row"]["spans"][0]["foreground"] == "#abcdef"
    assert sample["styled_widget_row"]["spans"][0]["background"] == "#123456"


@pytest.mark.parametrize("budget", [0, 1, 2, 4])
def test_border_collection_respects_shared_row_budget_and_reports_unsampled_edges(
    budget: int,
) -> None:
    async def exercise() -> None:
        app = StyledRowsApp()
        async with app.run_test(size=(40, 15)) as pilot:
            await pilot.pause()
            widget = app.query_one("#probe")
            composed = app.screen._compositor.render_strips()
            result = _collect(widget, composed, budget=budget)
            entry = result["regions"][0]
            raw_count = int("raw_content_row" in entry)
            assert len(entry["border_samples"]) + raw_count <= budget
            assert len(entry["widget_rows"]) == len(entry["border_samples"])
            assert result["remaining"] == 0
            assert "probe:row_limit" in result["omissions"]
            assert "probe:unsampled_edges" in result["omissions"]

    asyncio.run(exercise())


def test_border_cell_limit_and_missing_native_layer_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = _sample_widget()
    monkeypatch.setattr(diagnostics, "MAX_CELLS", 3)
    clips = diagnostics._border_sample_clips(widget.region)
    assert clips == [Region(0, 0, 3, 1)]
    assert diagnostics._border_coverage(widget.region, clips)["horizontal_cell_limit"]
    widget.render_lines = lambda _clip: []
    sample = diagnostics._border_sample(widget, [Strip.blank(20)] * 3, clips[0])
    assert sample["missing"] == [
        "styled_widget_row:IndexError",
        "styled_widget_row:unavailable_or_clipped",
    ]
    assert sample["styled_widget_row"] is None
    assert sample["compositor_row"]["text"] == " " * 3
    assert not sample["layers_equal"]
    assert diagnostics._border_sample_clips(Region(0, 0, 0, 0)) == []


@pytest.mark.parametrize("scene", ["connections-searching", "result-success"])
def test_border_scene_targets_are_actual_dialog_or_workspace_panes(scene: str) -> None:
    queries = []

    def query(selector: str) -> object:
        queries.append(selector)
        return object()

    app = SimpleNamespace(
        screen=SimpleNamespace(query_one=query),
        active_workspace=SimpleNamespace(query_one=query),
    )
    targets = diagnostics._targets(app, scene)
    assert scene in diagnostics.BORDER_SCENES
    assert queries == (
        ["#connections-picker"]
        if scene == "connections-searching"
        else [".query-pane", ".result-pane"]
    )
    assert [name for name, _widget, _rows in targets] == (
        ["connections-border"]
        if scene == "connections-searching"
        else ["query-pane", "result-pane"]
    )


def test_border_collector_retains_mismatches_and_original_compositor_strips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = _sample_widget()
    result: dict[str, Any] = {"regions": [], "missing": [], "omissions": []}
    monkeypatch.setattr(
        diagnostics, "_selector_child_state", lambda _widget: {"ancestors_truncated": False}
    )
    widget.display = widget.visible = True
    widget.content_region = widget.region
    widget.render_line = lambda _row: widget.render_lines(Region(0, 0, 6, 1))[0]
    strips = [Strip.blank(20), Strip.blank(20), Strip.blank(20)]
    before = deepcopy([list(strip) for strip in strips])
    remaining = diagnostics._collect_border_region(("probe", widget, 9), strips, result, 24)
    assert remaining == 22
    entry = result["regions"][0]
    assert len(entry["border_samples"]) == 1
    assert not entry["border_samples"][0]["layers_equal"]
    assert entry["composed_rows"][0]["text"] == " " * 6
    assert entry["widget_rows"][0]["text"] == "│界e\u0301 │"
    assert [list(strip) for strip in strips] == before


@pytest.mark.parametrize("scene", sorted(diagnostics.BORDER_SCENES))
def test_mounted_border_scenes_observe_real_panes_once_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scene: str
) -> None:
    async def exercise() -> None:
        app = sql_explorer_visual_scene.create_scene(
            scene, tmp_path / "scene.json", REPO_ROOT / sql_explorer_visual.MANIFEST
        )
        async with app.run_test(size=(208, 47)) as pilot:
            await wait_for_scene(app, pilot)
            targets = diagnostics._targets(app, scene)
            before = (
                app.focused,
                [(widget.region, widget.scroll_offset) for _, widget, _ in targets],
            )
            compositor = app.screen._compositor
            original = compositor.render_strips
            calls = []

            def render() -> list[Strip]:
                rows = original()
                calls.append(rows)
                return rows

            monkeypatch.setattr(compositor, "render_strips", render)
            result = diagnostics.terminal_diagnostics(app, scene)
            assert len(calls) == 1
            assert len(result["regions"]) == len(targets)
            assert sum(len(entry["border_samples"]) for entry in result["regions"]) <= 24
            for entry, (_name, widget, _count) in zip(result["regions"], targets):
                assert entry["geometry"]["region"] == diagnostics._region_record(widget.region)
                assert entry["geometry"]["ancestors"]
                assert entry["border_samples"]
                for sample in entry["border_samples"]:
                    clip = Region(**sample["widget_local_clip"])
                    _assert_native_sample(widget, calls[0], sample, clip)
            assert before == (
                app.focused,
                [(widget.region, widget.scroll_offset) for _, widget, _ in targets],
            )

    asyncio.run(exercise())


def _assert_native_sample(
    widget: Any, composed: list[Strip], sample: dict[str, Any], clip: Region
) -> None:
    x, y = widget.region.x + clip.x, widget.region.y + clip.y
    assert sample["screen_clip"] == diagnostics._region_record(Region(x, y, clip.width, 1))
    expected_widget = diagnostics.strip_record(
        widget.render_lines(clip)[0], x=x, y=y, width=clip.width
    )
    expected_composed = diagnostics.strip_record(
        composed[y].crop(x, x + clip.width), x=x, y=y, width=clip.width
    )
    assert sample["styled_widget_row"] == expected_widget
    assert sample["compositor_row"] == expected_composed
    assert sample["layers_equal"] == diagnostics.rendered_rows_equal(
        expected_widget, expected_composed
    )


def test_raw_layer_failure_keeps_available_border_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = _sample_widget()
    widget.display = widget.visible = True
    widget.content_region = widget.region

    def unavailable(_row: int) -> Strip:
        message = "raw layer unavailable"
        raise RuntimeError(message)

    widget.render_line = unavailable
    monkeypatch.setattr(
        diagnostics, "_selector_child_state", lambda _widget: {"ancestors_truncated": False}
    )
    result = _collect(widget, [Strip.blank(20)] * 3)
    assert result["missing"] == ["probe:raw_content:RuntimeError"]
    assert result["remaining"] == 22
    entry = result["regions"][0]
    assert "raw_content_row" not in entry
    assert entry["border_samples"][0]["styled_widget_row"]["text"] == "│界e\u0301 │"
    assert not entry["border_samples"][0]["layers_equal"]
