from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from rich.segment import Segment
from rich.style import Style
from textual.document._document import Selection
from textual.geometry import Region
from textual.strip import Strip

from agent_tools import sql_explorer_render_diagnostics as diagnostics
from agent_tools import sql_explorer_visual, sql_explorer_visual_scene
from tests._support.paths import REPO_ROOT
from tests.agent_tools._support import wait_for_scene

if TYPE_CHECKING:
    from pathlib import Path


def _state(app: Any) -> tuple[Any, ...]:
    editor = app.active_workspace.editor
    return (
        editor.text,
        editor.cursor_selections,
        tuple(id(selection) for selection in editor.cursor_selections),
        dict(editor._cursor_preferences),
        app.focused,
        editor.scroll_offset,
        app.keyboard_mode,
    )


def _assert_caret_layers(app: Any, caret: dict[str, Any], composed: Any) -> None:
    editor = app.active_workspace.editor
    row, column = caret["end"]
    assert caret["start"] == [row, column]
    assert caret["empty"]
    assert caret["preferred_column"] == editor._cursor_preferences.get(
        editor.cursor_selections[caret["index"]]
    )
    assert caret["wrapped_offset"] == [14, row]
    assert caret["content_local_cell"] == [editor.gutter_width + 14, row]
    screen_x, screen_y = caret["screen_cell"]
    assert screen_x == editor.content_region.x + editor.gutter_width + 14
    assert screen_y == editor.content_region.y + row
    assert caret["visible"]
    assert not caret["clipped"]
    local_x, local_y = caret["content_local_cell"]
    strips = {
        "widget_rows": editor.render_line(local_y).crop(local_x, local_x + 1),
        "composed_rows": composed[screen_y].crop(screen_x, screen_x + 1),
    }
    for layer, strip in strips.items():
        observed = caret[layer]
        assert observed == diagnostics.strip_record(strip, x=screen_x, y=screen_y, width=1)
        assert observed["text"] == editor.document[row][column]
        assert all(
            isinstance(span["flags"][flag], bool)
            for span in observed["spans"]
            for flag in ("underline", "bold", "reverse")
        )


def test_mounted_multicursor_observation_preserves_state_and_maps_all_layers(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        app = sql_explorer_visual_scene.create_scene(
            "keyboard-multicursor",
            tmp_path / "geometry.json",
            REPO_ROOT / sql_explorer_visual.MANIFEST,
        )
        assert isinstance(app, sql_explorer_visual_scene.VisualExplorerApp)
        async with app.run_test(size=(208, 47)) as pilot:
            await wait_for_scene(app, pilot)
            before = _state(app)
            result = diagnostics.terminal_diagnostics(app, "keyboard-multicursor")
            assert before == _state(app)
            assert not result["missing"]
            assert not result["omissions"]
            snapshot = result["multicursor"]
            assert snapshot["keyboard_mode"] is True
            assert snapshot["active_workspace"] == app.active_workspace.tab_id
            assert snapshot["focused_widget"]["identity"] == id(app.focused)
            assert snapshot["cursor_count"] == 3
            assert [caret["end"] for caret in snapshot["carets"]] == [[0, 14], [1, 14], [2, 14]]
            assert [caret["primary"] for caret in snapshot["carets"]] == [False, True, False]
            assert snapshot["primary_identity"] == snapshot["carets"][1]["identity"]
            assert snapshot["expected"]["primary"] == [1, 14]
            composed = app.screen._compositor.render_strips()
            for caret in snapshot["carets"]:
                _assert_caret_layers(app, caret, composed)
            assert [entry["name"] for entry in result["regions"]] == [
                "multicursor-editor",
                "multicursor-ruler",
            ]
            assert [len(entry["widget_rows"]) for entry in result["regions"]] == [3, 1]
            assert before == _state(app)

    asyncio.run(exercise())


@pytest.mark.parametrize(("line", "column"), [("界e\u0301\tZ", 3), ("界\tZ", 0), ("界\tZ", 2)])
def test_mounted_unicode_and_tab_mapping_uses_terminal_cells(
    tmp_path: Path, line: str, column: int
) -> None:
    async def exercise() -> None:
        app = sql_explorer_visual_scene.create_scene(
            "keyboard-multicursor",
            tmp_path / "geometry.json",
            REPO_ROOT / sql_explorer_visual.MANIFEST,
        )
        assert isinstance(app, sql_explorer_visual_scene.VisualExplorerApp)
        async with app.run_test(size=(208, 47)) as pilot:
            await wait_for_scene(app, pilot)
            editor = app.active_workspace.editor
            editor.text = line + "\n" + line
            editor._set_selections(Selection.cursor((0, column)), [Selection.cursor((1, column))])
            await pilot.pause()
            before = _state(app)
            result = diagnostics.terminal_diagnostics(app, "keyboard-multicursor")
            assert not result["missing"]
            for caret in result["multicursor"]["carets"]:
                observed_row, observed_column = caret["end"]
                offset = editor.wrapped_document.location_to_offset((observed_row, observed_column))
                assert caret["wrapped_offset"] == list(offset)
                x, y = caret["content_local_cell"]
                width = caret["cell_width"]
                original = editor.render_line(y).crop(x, x + width)
                for layer in ("widget_rows", "composed_rows"):
                    assert caret[layer]["text"] == original.text
                    assert caret[layer]["cells"] == original.cell_length == width
                    assert not caret[layer]["truncated"]
            assert before == _state(app)
            assert "multicursor:unexpected_positions" in result["multicursor"]["findings"]

    asyncio.run(exercise())


def _fake_observation() -> tuple[Any, dict[str, Any]]:
    selections = tuple(Selection.cursor((row, 14)) for row in range(3))
    editor = SimpleNamespace(
        selection=selections[1],
        cursor_selections=selections,
        cursor_count=3,
        document=["0123456789" * 3] * 3,
        wrapped_document=SimpleNamespace(location_to_offset=lambda point: (point[1], point[0])),
        scroll_x=0,
        scroll_y=0,
        scroll_offset=(0, 0),
        region=Region(2, 5, 40, 10),
        content_region=Region(2, 5, 40, 10),
        scrollable_content_region=Region(2, 5, 40, 10),
        gutter_width=3,
        gutter=SimpleNamespace(left=0),
        has_focus=True,
        _cursor_preferences=dict.fromkeys(selections, 14),
    )
    rows = [
        diagnostics.strip_record(
            Strip([Segment(" " * 40, Style(color="#abcdef", underline=True))]),
            x=2,
            y=5 + row,
            width=40,
        )
        for row in range(3)
    ]
    result: dict[str, Any] = {
        "regions": [
            {
                "name": "multicursor-editor",
                "widget_rows": deepcopy(rows),
                "composed_rows": deepcopy(rows),
            }
        ],
        "missing": [],
        "omissions": [],
    }
    app = SimpleNamespace(
        active_workspace=SimpleNamespace(tab_id="1", editor=editor),
        keyboard_mode=True,
        focused=SimpleNamespace(id="query-editor"),
    )
    return app, result


@pytest.mark.parametrize("layer", ["widget_rows", "composed_rows"])
@pytest.mark.parametrize("damage", ["missing_row", "missing_flag", "lost_underline"])
def test_missing_layer_and_lost_style_are_observed_separately(layer: str, damage: str) -> None:
    app, result = _fake_observation()
    rows = result["regions"][0][layer]
    if damage == "missing_row":
        rows.pop(0)
    elif damage == "missing_flag":
        del rows[0]["spans"][0]["flags"]["underline"]
    else:
        rows[0]["spans"][0]["flags"]["underline"] = False
    before = deepcopy(result)
    diagnostics._multicursor_diagnostics(app, result)
    assert result["regions"] == before["regions"]
    caret = result["multicursor"]["carets"][0]
    if damage == "lost_underline":
        assert not result["missing"]
        assert caret[layer]["spans"][0]["flags"]["underline"] is False
        assert result["multicursor"]["findings"] == [f"multicursor:caret:0:{layer}:caret_mark_absent"]
    else:
        assert result["missing"]
        assert not result["multicursor"]["findings"]


@pytest.mark.parametrize("axis", ["x", "y"])
def test_offscreen_carets_keep_mapping_and_explicitly_incomplete_cells(axis: str) -> None:
    app, result = _fake_observation()
    editor = app.active_workspace.editor
    setattr(editor, "scroll_" + axis, 20)
    diagnostics._multicursor_diagnostics(app, result)
    assert all(caret["clipped"] for caret in result["multicursor"]["carets"])
    assert all(not caret["visible"] for caret in result["multicursor"]["carets"])
    assert "multicursor:caret:0:clipped" in result["missing"]


def test_merged_missing_and_excess_cursors_do_not_inherit_expected_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, result = _fake_observation()
    editor = app.active_workspace.editor
    editor.cursor_selections = (editor.selection,)
    editor.cursor_count = 1
    diagnostics._multicursor_diagnostics(app, result)
    snapshot = result["multicursor"]
    assert snapshot["cursor_count"] == 1
    assert len(snapshot["carets"]) == 1
    assert snapshot["expected"]["positions"] == [[0, 14], [1, 14], [2, 14]]
    assert snapshot["findings"] == ["multicursor:unexpected_positions"]

    app, result = _fake_observation()
    monkeypatch.setattr(diagnostics, "MAX_REGIONS", 2)
    diagnostics._multicursor_diagnostics(app, result)
    assert result["multicursor"]["cursor_count"] == 3
    assert len(result["multicursor"]["carets"]) == 2
    assert result["omissions"] == ["multicursor:caret_limit"]


def test_unavailable_state_and_mapping_probes_are_explicit() -> None:
    app, result = _fake_observation()
    del app.active_workspace.editor.wrapped_document
    diagnostics._multicursor_diagnostics(app, result)
    assert result["multicursor"]["cursor_count"] == 3
    assert len(result["missing"]) == 3
    assert all("AttributeError" in message for message in result["missing"])
    app, result = _fake_observation()
    del app.keyboard_mode
    diagnostics._multicursor_diagnostics(app, result)
    assert result["missing"] == ["multicursor:state:AttributeError"]


@pytest.mark.parametrize(
    ("limit", "value"), [("MAX_ROWS", 1), ("MAX_CELLS", 8), ("MAX_REGIONS", 1)]
)
def test_scene_collection_limits_cannot_report_complete_caret_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str, value: int
) -> None:
    async def exercise() -> None:
        app = sql_explorer_visual_scene.create_scene(
            "keyboard-multicursor",
            tmp_path / "geometry.json",
            REPO_ROOT / sql_explorer_visual.MANIFEST,
        )
        assert isinstance(app, sql_explorer_visual_scene.VisualExplorerApp)
        async with app.run_test(size=(208, 47)) as pilot:
            await wait_for_scene(app, pilot)
            before = _state(app)
            monkeypatch.setattr(diagnostics, limit, value)
            result = diagnostics.terminal_diagnostics(app, "keyboard-multicursor")
            assert result["missing"] or result["omissions"]
            assert before == _state(app)
            assert len(result["regions"]) <= diagnostics.MAX_REGIONS
            row_count = sum(len(entry["widget_rows"]) for entry in result["regions"])
            assert row_count <= diagnostics.MAX_ROWS

    asyncio.run(exercise())


def test_cell_crop_retains_combining_wide_and_expanded_tab_styles() -> None:
    row = diagnostics.strip_record(
        Strip([Segment("界e\u0301    Z", Style(color="#123456", underline=True))]),
        x=7,
        y=4,
        width=20,
    )
    wide = diagnostics._caret_cells([row], x=7, y=4, width=2)
    assert wide is not None
    assert wide["text"] == "界"
    assert wide["cells"] == 2
    combining = diagnostics._caret_cells([row], x=9, y=4, width=1)
    assert combining is not None
    assert combining["text"] == "e\u0301"
    spaces = diagnostics._caret_cells([row], x=10, y=4, width=4)
    assert spaces is not None
    assert spaces["text"] == " " * 4
    assert spaces["spans"][0]["flags"]["underline"]
    assert spaces["spans"][0]["foreground"] == "#123456"
    for cropped in (wide, combining, spaces):
        assert cropped["spans"][0]["foreground"] == row["spans"][0]["foreground"]
        assert cropped["spans"][0]["background"] == row["spans"][0]["background"]
        assert cropped["spans"][0]["flags"] == row["spans"][0]["flags"]


@pytest.mark.parametrize("scene", ["keyboard-multicursor", "editor-command-caret"])
def test_unavailable_compositor_retains_actual_cursor_state(scene: str) -> None:
    app, result = _fake_observation()
    app.active_workspace.query_one = lambda _query: object()

    def missing_strips() -> Any:
        message = "compositor unavailable"
        raise RuntimeError(message)

    app.screen = SimpleNamespace(_compositor=SimpleNamespace(render_strips=missing_strips))
    observed = diagnostics.terminal_diagnostics(app, scene)
    assert "terminal:RuntimeError" in observed["missing"]
    assert observed["multicursor"]["cursor_count"] == 3
    assert [caret["end"] for caret in observed["multicursor"]["carets"]] == [
        [0, 14],
        [1, 14],
        [2, 14],
    ]
    assert all(caret["composed_rows"] is None for caret in observed["multicursor"]["carets"])
    assert result["missing"] == []


def test_svg_caret_decoration_attributes_survive_reduced_layer_evidence() -> None:
    svg = (
        '<svg><defs><clipPath id="terminal-line-5"><rect y="0" height="20"/>'
        '</clipPath></defs><g clip-path="url(#terminal-line-5)" text-decoration="underline">'
        '<text>界</text></g></svg>'
    )
    terminal = {"regions": [{"composed_rows": [{"y": 5}]}]}
    result = diagnostics.svg_diagnostics(svg, terminal)
    text = next(element for element in result["elements"] if element["tag"] == "text")
    assert text["text"] == "界"
    assert text["effective_style"]["text-decoration"] == "underline"
    assert text["ancestor_clips"] == ["url(#terminal-line-5)"]
    assert not result["missing"]


def test_padding_marks_and_state_mismatches_are_reported_without_repair() -> None:
    app, result = _fake_observation()
    editor = app.active_workspace.editor
    editor.document = ["x" * 14] * 3
    editor.selection = editor.cursor_selections[0]
    app.keyboard_mode = False
    app.focused = None
    rows = [
        diagnostics.strip_record(
            Strip([Segment(" " * 17 + "│" + " " * 22)]),
            x=2,
            y=5 + row,
            width=40,
        )
        for row in range(3)
    ]
    for layer in ("widget_rows", "composed_rows"):
        result["regions"][0][layer] = deepcopy(rows)
    diagnostics._multicursor_diagnostics(app, result)
    snapshot = result["multicursor"]
    assert snapshot["focused_widget"] is None
    assert snapshot["findings"] == [
        "multicursor:unexpected_primary",
        "multicursor:unexpected_keyboard_mode",
    ]
    assert not result["missing"]
    assert all(not caret["in_document"] for caret in snapshot["carets"])
    assert all(caret["widget_rows"]["text"] == "│" for caret in snapshot["carets"])
    assert app.keyboard_mode is False
    assert editor.selection is editor.cursor_selections[0]


@pytest.mark.parametrize(
    ("x", "width", "expected"),
    [(8, 1, "e\u0301\u0327"), (7, 1, " "), (6, 1, " "), (9, 1, "Z")],
)
def test_caret_crop_keeps_cross_span_marks_only_with_their_complete_base(
    x: int, width: int, expected: str
) -> None:
    base_style = Style(color="#123456", bgcolor="#345678", underline=True)
    mark_style = Style(color="#abcdef", bgcolor="#654321", italic=True)
    row = diagnostics.strip_record(
        Strip(
            [
                Segment("界e", base_style),
                Segment("\u0301\u0327", mark_style),
                Segment("Z", base_style),
            ]
        ),
        x=6,
        y=3,
        width=20,
    )
    original = deepcopy(row)
    cropped = diagnostics._caret_cells([row], x=x, y=3, width=width)
    assert cropped is not None
    assert cropped["text"] == expected
    assert cropped["cells"] == width
    assert not cropped["truncated"]
    assert row == original
    if x == 8:
        assert [span["text"] for span in cropped["spans"]] == ["e", "\u0301\u0327"]
        assert [span["cells"] for span in cropped["spans"]] == [1, 0]
        assert cropped["spans"][0]["foreground"] == "#123456"
        assert cropped["spans"][0]["background"] == "#345678"
        assert cropped["spans"][0]["flags"]["underline"]
        assert cropped["spans"][1]["foreground"] == "#abcdef"
        assert cropped["spans"][1]["background"] == "#654321"
        assert cropped["spans"][1]["flags"]["italic"]
