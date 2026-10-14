from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.segment import Segment
from rich.style import Style
from textual.css.query import NoMatches
from textual.strip import Strip
from textual.widgets import Checkbox

from agent_tools import sql_explorer_render_diagnostics as diagnostics
from agent_tools import sql_explorer_visual, sql_explorer_visual_capture, sql_explorer_visual_scene
from tests._support.paths import REPO_ROOT
from tests.agent_tools._support import wait_for_scene


def test_strip_records_unicode_cells_and_resolved_styles() -> None:
    strip = Strip([Segment("界e\u0301", Style(color="#123456", bgcolor="#abcdef", underline=True))])
    row = diagnostics.strip_record(strip, x=7, y=3, width=208)
    assert row["text"] == "界e\u0301"
    assert row["cells"] == 3
    assert row["spans"][0]["x"] == 7
    assert row["spans"][0]["cells"] == 3
    assert row["spans"][0]["foreground"] == "#123456"
    assert row["spans"][0]["background"] == "#abcdef"
    assert row["spans"][0]["flags"]["underline"]
    long = diagnostics.strip_record(Strip([Segment("a" * 209)]), x=0, y=0, width=209)
    assert long["cells"] == 208
    assert long["truncated"]


def _svg(text: str = "Heading") -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 200">'
        '<style>.r1 {fill: #123456; font-weight: bold}</style>'
        '<defs><clipPath id="terminal-line-0"><rect y="0" height="20" /></clipPath>'
        '<clipPath id="screen"><rect width="400" height="200" /></clipPath></defs>'
        '<g transform="translate(10,20)" clip-path="url(#screen)">'
        '<rect y="0" width="100" height="20" fill="#abcdef" />'
        '<g clip-path="url(#terminal-line-0)">'
        f'<text class="r1" x="0" y="15"><tspan>{text}</tspan></text>'
        '</g></g></svg>'
    )


def _terminal() -> dict[str, Any]:
    return {
        "regions": [{"composed_rows": [{"y": 0}]}],
        "missing": [],
        "omissions": [],
    }


def test_svg_retains_inherited_styles_transforms_clips_and_backgrounds() -> None:
    result = diagnostics.svg_diagnostics(_svg(), _terminal())
    assert not result["missing"]
    text = next(item for item in result["elements"] if item["tag"] == "tspan")
    assert text["text"] == "Heading"
    assert text["effective_style"]["fill"] == "#123456"
    assert text["effective_style"]["font-weight"] == "bold"
    assert text["ancestor_transforms"] == ["translate(10,20)"]
    assert text["ancestor_clips"] == ["url(#screen)", "url(#terminal-line-0)"]
    ids = {item["attributes"].get("id") for item in result["elements"]}
    assert {"screen", "terminal-line-0"} <= ids
    assert any(item["attributes"].get("fill") == "#abcdef" for item in result["elements"])
    missing = diagnostics.svg_diagnostics("<svg />", _terminal())
    assert missing["missing"] == ["svg:line_clip_mapping:0"]


def test_svg_and_json_bounds_explicitly_invalidate_incomplete_evidence() -> None:
    svg = _svg().replace("<tspan>Heading</tspan>", "<tspan>Heading</tspan>" * 300)
    result = diagnostics.svg_diagnostics(svg, _terminal())
    assert len(result["elements"]) == diagnostics.MAX_SVG_ELEMENTS
    assert result["omitted_elements"] > 0
    payload = {
        "complete": True,
        "omissions": [],
        "terminal": {"regions": []},
        "svg": {"elements": [{"text": "a" * diagnostics.MAX_BYTES}], "omitted_elements": 0},
    }
    bounded = diagnostics.bounded_payload(payload)
    assert not bounded["complete"]
    assert bounded["omissions"] == ["byte_limit"]
    assert len(json.dumps(bounded, indent=4).encode()) <= diagnostics.MAX_BYTES


def _highlighted_runs(row: dict[str, Any]) -> list[str]:
    runs: list[str] = []
    end = -1
    for span in row["spans"]:
        if span["background"] != "#ffff00":
            end = -1
            continue
        if span["x"] == end:
            runs[-1] += span["text"]
        else:
            runs.append(span["text"])
        end = span["x"] + span["cells"]
    return runs


def _assert_find_scene(terminal: dict[str, Any]) -> None:
    assert len(terminal["editor"]["matches"]) == 3
    assert terminal["editor"]["selections"]
    region = terminal["regions"][0]
    for layer in ("widget_rows", "composed_rows"):
        assert all(
            any("order" in text for text in _highlighted_runs(row))
            for row in region[layer][:3]
        )


@pytest.mark.parametrize("broken", [False, True])
def test_find_evidence_accepts_split_carets_but_rejects_a_highlight_gap(broken: bool) -> None:
    spans = [
        Segment("or", Style(bgcolor="#ffff00")),
        Segment("d", Style(bgcolor="#20252a" if broken else "#ffff00", underline=True)),
        Segment("er", Style(bgcolor="#ffff00")),
    ]
    row = diagnostics.strip_record(Strip(spans), x=0, y=0, width=5)
    assert ("order" in _highlighted_runs(row)) is not broken


def _assert_scrollbar_scene(terminal: dict[str, Any]) -> None:
    model = terminal["editor"]
    assert model["scroll_offset"][0] > 0
    assert model["scrollbar"]["position"] == model["scroll_offset"][0]
    assert model["scrollbar"]["window_virtual_size"] == model["content_size"][0]
    assert model["scrollbar"]["window_size"] == model["viewport"]["width"]


def _assert_form_scene(terminal: dict[str, Any]) -> None:
    flags = terminal["regions"][:3]
    assert all(region["state"]["value"] is False for region in flags)
    assert all(region["state"]["button_text"] == "[ ]" for region in flags)
    for region in flags:
        for layer in ("widget_rows", "composed_rows"):
            assert region[layer][0]["text"].startswith("[ ] ")
            assert region[layer][0]["text"].rstrip().endswith("False")
    for region in terminal["regions"][3:]:
        if not region["name"].startswith("remove-column-"):
            continue
        for layer in ("widget_rows", "composed_rows"):
            assert region[layer][0]["text"].strip() == "x"


def _assert_completion_scene(terminal: dict[str, Any]) -> None:
    placement = terminal["completion"]
    popup, row = placement["popup"], placement["active_row"]
    assert popup["y"] > row or popup["y"] + popup["height"] <= row


@pytest.mark.parametrize("scene", sorted(diagnostics.SCENES))
def test_target_scenes_collect_only_bounded_widget_and_composed_regions(
    scene: str, tmp_path: Path
) -> None:
    async def exercise() -> None:
        app = sql_explorer_visual_scene.create_scene(
            scene, tmp_path / "geometry.json", REPO_ROOT / sql_explorer_visual.MANIFEST
        )
        async with app.run_test(size=(208, 47)) as pilot:
            await wait_for_scene(app, pilot)
            terminal = diagnostics.terminal_diagnostics(app, scene)
            assert not terminal["missing"]
            assert not terminal["omissions"]
            assert 1 <= len(terminal["regions"]) <= diagnostics.MAX_REGIONS
            assert sum(len(region["composed_rows"]) for region in terminal["regions"]) <= 24
            for region in terminal["regions"]:
                if region["name"].startswith("source-") and not (
                    region["state"]["display"]
                    and region["region"]["width"] > 0
                    and region["region"]["height"] > 0
                ):
                    continue
                assert region["widget_rows"]
                assert region["composed_rows"]
                assert all(row["cells"] <= 208 for row in region["composed_rows"])
            if scene == "find-replace":
                _assert_find_scene(terminal)
            if scene == "column-ruler-scrolled":
                _assert_scrollbar_scene(terminal)
            if scene.startswith("create-table"):
                _assert_form_scene(terminal)
            if scene in diagnostics.SELECTOR_SCENES:
                snapshot = diagnostics.selector_readiness_snapshot(app, scene, terminal=terminal)
                assert all(diagnostics.selector_readiness_assertions(snapshot).values()), snapshot
            if scene == "completion-columns":
                _assert_completion_scene(terminal)

    asyncio.run(exercise())


def test_final_capture_exports_once_hashes_same_frame_and_survives_geometry_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exports = []
    selector = {"visibility_ok": False, "findings": ["source-label:empty_content_region"]}
    terminal = _terminal()
    terminal["source_selector"] = selector
    app: Any = SimpleNamespace(
        size=SimpleNamespace(width=208, height=47),
        console=SimpleNamespace(color_system="256"),
    )

    def export_screenshot(**_kwargs: Any) -> str:
        exports.append(_svg())
        return exports[-1]

    app.export_screenshot = export_screenshot
    evidence, screenshot = tmp_path / "geometry.json", tmp_path / "scene.png"
    geometry = {"schema_version": 1, "ok": True, "assertions": {"unchanged": True}}

    def write_evidence(*_args: Any) -> None:
        evidence.write_text(json.dumps(geometry), encoding="utf-8")

    def write_png(svg: str, path: Path, *, framed_svg: str | None = None) -> None:
        assert svg == exports[0]
        assert framed_svg == sql_explorer_visual_capture._frame_svg(exports[0])
        path.write_bytes(b"same-frame-png")

    monkeypatch.setattr(sql_explorer_visual_capture, "_write_evidence", write_evidence)
    monkeypatch.setattr(sql_explorer_visual_capture, "_write_png", write_png)
    monkeypatch.setattr(
        sql_explorer_visual_capture, "terminal_diagnostics", lambda *_args: terminal
    )
    sql_explorer_visual_capture._capture_frame(
        app, "database-picker", evidence, tmp_path / "unused", screenshot
    )
    assert len(exports) == 1
    saved = json.loads(evidence.read_text(encoding="utf-8"))
    assert saved["selector_readiness"]["collection_complete"] is True
    assert saved["selector_readiness"]["selector"] == selector
    assert saved["selector_readiness"]["selector"]["visibility_ok"] is False
    assert saved["render_diagnostics"]["terminal"]["source_selector"] == selector
    assert saved["schema_version"] == 1
    assert saved["ok"] is True
    assert saved["assertions"] == geometry["assertions"]
    hashes = saved["render_diagnostics"]["hashes"]
    assert hashes["raw_svg_sha256"] == diagnostics.digest(exports[0].encode())
    framed = sql_explorer_visual_capture._frame_svg(exports[0])
    assert hashes["framed_svg_sha256"] == diagnostics.digest(framed.encode())
    assert hashes["png_sha256"] == diagnostics.digest(screenshot.read_bytes())
    before = evidence.read_bytes()
    # No app/manifest access should happen when timers or teardown revisit a final frame.
    unused_app: Any = object()
    sql_explorer_visual_scene._write_evidence(
        unused_app, "database-picker", evidence, tmp_path / "missing-manifest"
    )
    assert evidence.read_bytes() == before
    assert not evidence.with_suffix(".tmp").exists()


def test_failed_atomic_replace_preserves_legacy_geometry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    geometry = {"schema_version": 1, "ok": True, "assertions": {"kept": True}}
    evidence, screenshot = tmp_path / "geometry.json", tmp_path / "scene.png"
    app: Any = SimpleNamespace(
        size=SimpleNamespace(width=208, height=47),
        console=SimpleNamespace(color_system="256"),
        export_screenshot=lambda **_kwargs: _svg(),
    )

    def write_evidence(*_args: Any) -> None:
        evidence.write_text(json.dumps(geometry), encoding="utf-8")

    def replace(_source: Path, _destination: Path) -> None:
        message = "simulated atomic replacement failure"
        raise OSError(message)

    monkeypatch.setattr(sql_explorer_visual_capture, "_write_evidence", write_evidence)
    monkeypatch.setattr(
        sql_explorer_visual_capture,
        "_write_png",
        lambda _svg, path, **_kwargs: path.write_bytes(b"png"),
    )
    monkeypatch.setattr(
        sql_explorer_visual_capture, "terminal_diagnostics", lambda *_args: _terminal()
    )
    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(OSError, match="replacement failure"):
        sql_explorer_visual_capture._capture_frame(
            app, "database-picker", evidence, tmp_path / "unused", screenshot
        )
    assert json.loads(evidence.read_text(encoding="utf-8")) == geometry


def test_checkbox_diagnostics_track_true_disabled_and_focused_states(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = sql_explorer_visual_scene.create_scene(
            "create-table-advanced",
            tmp_path / "geometry.json",
            REPO_ROOT / sql_explorer_visual.MANIFEST,
        )
        async with app.run_test(size=(208, 47)) as pilot:
            await wait_for_scene(app, pilot)
            checkbox = app.screen.query_one("#create-skip_if_exists", Checkbox)
            checkbox.value = True
            checkbox.focus()
            await pilot.pause()
            terminal = diagnostics.terminal_diagnostics(app, "create-table-advanced")
            flags = {region["name"]: region for region in terminal["regions"][:3]}
            assert not flags["insert_data"]["state"]["enabled"]
            assert not flags["insert_data"]["state"]["value"]
            assert flags["skip_if_exists"]["state"]["focused"]
            assert flags["skip_if_exists"]["state"]["value"]
            assert flags["skip_if_exists"]["state"]["button_text"] == "[x]"
            assert flags["skip_if_exists"]["composed_rows"]

    asyncio.run(exercise())


def test_region_row_limits_and_missing_rows_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    region = SimpleNamespace(x=0, y=0, width=208, height=4)
    widget = SimpleNamespace(
        region=region,
        id="bounded",
        disabled=False,
        has_focus=False,
        display=True,
        render_lines=lambda _region: [Strip([Segment("widget")])] * 4,
    )
    app = SimpleNamespace(
        screen=SimpleNamespace(
            _compositor=SimpleNamespace(render_strips=lambda: [Strip([Segment("composed")])] * 47)
        )
    )
    monkeypatch.setattr(
        diagnostics,
        "_targets",
        lambda *_args: [(str(index), widget, 4) for index in range(diagnostics.MAX_REGIONS + 1)],
    )
    result = diagnostics.terminal_diagnostics(app, "database-picker")
    assert len(result["regions"]) == diagnostics.MAX_REGIONS
    assert sum(len(item["composed_rows"]) for item in result["regions"]) == 24
    assert "region_limit" in result["omissions"]
    assert result["missing"]
    assert result["regions"][0]["widget_rows"][0]["text"] == "widget"
    assert result["regions"][0]["composed_rows"][0]["text"] == "composed"
    monkeypatch.setattr(diagnostics, "_targets", lambda *_args: list())
    assert diagnostics.terminal_diagnostics(app, "database-picker")["missing"]


def test_missing_and_truncated_diagnostics_never_claim_completeness() -> None:
    app = SimpleNamespace(
        size=SimpleNamespace(width=208, height=47),
        console=SimpleNamespace(color_system="256"),
    )
    terminal = _terminal()
    terminal["missing"] = ["heading:rows"]
    terminal["omissions"] = ["cell_limit"]
    result = diagnostics.render_diagnostics(
        app, "database-picker", _svg(), _svg(), b"png", terminal
    )
    assert result["complete"] is False
    assert "heading:rows" in result["missing"]
    assert "cell_limit" in result["omissions"]
    assert result["svg"]["viewBox"] == "0 0 400 200"


@pytest.mark.parametrize(
    ("display", "width", "height"), [(False, 8, 1), (True, 0, 1), (True, 8, 0)]
)
def test_invisible_required_region_invalidates_terminal_evidence(
    monkeypatch: pytest.MonkeyPatch, display: bool, width: int, height: int
) -> None:
    widget = SimpleNamespace(
        region=SimpleNamespace(x=0, y=0, width=width, height=height),
        id="heading",
        disabled=False,
        has_focus=False,
        display=display,
        render=lambda: "Heading",
    )
    app = SimpleNamespace(
        screen=SimpleNamespace(_compositor=SimpleNamespace(render_strips=list))
    )
    monkeypatch.setattr(diagnostics, "_targets", lambda *_args: [("heading", widget, 1)])
    result = diagnostics.terminal_diagnostics(app, "database-picker")
    assert result["missing"] == ["heading:not_visible"]
    assert result["regions"][0]["composed_rows"] == []


def test_truncated_widget_and_incomplete_composed_rows_invalidate_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = SimpleNamespace(
        region=SimpleNamespace(x=0, y=0, width=8, height=1),
        id="heading",
        disabled=False,
        has_focus=False,
        display=True,
        render=lambda: "Heading",
        render_lines=lambda _region: [Strip([Segment("Heading too long")])],
    )
    app = SimpleNamespace(
        screen=SimpleNamespace(
            _compositor=SimpleNamespace(render_strips=lambda: [Strip([Segment("short")])])
        )
    )
    monkeypatch.setattr(diagnostics, "_targets", lambda *_args: [("heading", widget, 1)])
    result = diagnostics.terminal_diagnostics(app, "database-picker")
    assert result["omissions"] == ["heading:row_truncated"]
    assert result["missing"] == ["heading:incomplete_screen_row"]
    assert result["regions"][0]["widget_rows"][0]["cells"] == 8


def test_missing_widget_records_missing_evidence_instead_of_aborting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*_args: Any) -> Any:
        message = "required diagnostic widget absent"
        raise NoMatches(message)

    monkeypatch.setattr(diagnostics, "_targets", missing)
    result = diagnostics.terminal_diagnostics(object(), "database-picker")
    assert result["missing"] == ["terminal:NoMatches"]
    assert result["regions"] == []


@pytest.mark.parametrize("scene", sorted(diagnostics.SELECTOR_SCENES))
def test_clipped_selector_remains_observable_but_blocks_readiness_and_final_geometry(
    scene: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        evidence = tmp_path / "geometry.json"
        manifest = REPO_ROOT / sql_explorer_visual.MANIFEST
        app = sql_explorer_visual_scene.VisualExplorerApp(scene, evidence, manifest)
        async with app.run_test(size=(208, 47)) as pilot:
            await wait_for_scene(app, pilot)
            app._poll_scene_readiness()
            assert sql_explorer_visual_capture._scene_ready(app)
            selector = app.screen.query_one("#create-table-source")
            # Reintroduce the confirmed double frame: the outer content shrinks to one row.
            selector.styles.border = ("solid", "red")
            await pilot.pause()
            app._poll_scene_readiness()
            snapshot = app._selector_readiness_snapshot()
            assert snapshot["collection_complete"]
            assert not snapshot["selector"]["visibility_ok"]
            assert "source-label:ancestor_clipping" in snapshot["selector"]["findings"]
            findings = snapshot["selector"]["findings"]
            assert "source-label:composed_rows:absent_expected_text" in findings
            assert not sql_explorer_visual_capture._scene_ready(app)
            timeout = sql_explorer_visual_capture._timeout_diagnostics(
                app, scene, evidence, manifest
            )
            assert timeout["selector_readiness"]["selector"]["findings"] == findings
            assert not timeout["geometry"]["ok"]
            assert "source_selector_visible" in timeout["geometry"]["failed_assertions"]
            evidence.write_text('{"ok": true, "render_diagnostics": {}}', encoding="utf-8")
            png = tmp_path / "scene.png"
            with pytest.raises(RuntimeError, match="not ready"):
                sql_explorer_visual_capture._capture_frame(app, scene, evidence, manifest, png)
            assert not evidence.exists()
            assert not png.exists()
            # Even a stale/incorrect readiness verdict cannot bypass final geometry.
            monkeypatch.setattr(sql_explorer_visual_capture, "_scene_ready", lambda _app: True)
            evidence.write_text('{"ok": true, "render_diagnostics": {}}', encoding="utf-8")
            with pytest.raises(RuntimeError, match="failed geometry"):
                sql_explorer_visual_capture._capture_frame(app, scene, evidence, manifest, png)
            failed = json.loads(evidence.read_text(encoding="utf-8"))
            assert not failed["ok"]
            assert not failed["assertions"]["source_selector_visible"]
            assert failed["selector_readiness"]["selector"]["findings"]
            assert not png.exists()

    asyncio.run(exercise())


@pytest.mark.parametrize("error_type", [AttributeError, IndexError, RuntimeError])
def test_region_render_failure_retains_evidence_and_collects_later_regions(
    error_type: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_region: Any) -> list[Strip]:
        message = "diagnostic render failed"
        raise error_type(message)

    region = SimpleNamespace(x=0, y=0, width=8, height=1)
    state = {"region": region, "disabled": False, "has_focus": False, "display": True}
    broken = SimpleNamespace(id="broken", render_lines=fail, **state)
    healthy = SimpleNamespace(
        id="healthy", render_lines=lambda _region: [Strip([Segment("healthy ")])], **state
    )
    app = SimpleNamespace(
        screen=SimpleNamespace(
            _compositor=SimpleNamespace(render_strips=lambda: [Strip([Segment("healthy ")])])
        )
    )
    monkeypatch.setattr(
        diagnostics, "_targets", lambda *_args: [("broken", broken, 1), ("healthy", healthy, 1)]
    )
    result = diagnostics.terminal_diagnostics(app, "database-picker")
    assert result["missing"] == [f"broken:{error_type.__name__}"]
    assert not result["omissions"]
    assert result["regions"][0]["widget_rows"] == []
    assert result["regions"][1]["widget_rows"][0]["text"] == "healthy "
    assert result["regions"][1]["composed_rows"][0]["text"] == "healthy "
