from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock

import pytest

from agent_tools import sql_explorer_visual_capture as capture

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("ready", [False, True])
def test_final_capture_rechecks_state_and_discards_stale_geometry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ready: bool
) -> None:
    evidence = tmp_path / "geometry.json"
    evidence.write_text('{"ok": true, "render_diagnostics": {}}', encoding="utf-8")
    exported = []
    validated = []
    app: Any = SimpleNamespace(
        export_screenshot=lambda **_kwargs: exported.append(True) or "<svg/>"
    )
    monkeypatch.setattr(capture, "_scene_ready", lambda _app: ready)

    def validate(*_args: object) -> None:
        assert not evidence.exists()
        validated.append(True)
        evidence.write_text(
            json.dumps({"ok": False, "assertions": {"present:#required": False}}),
            encoding="utf-8",
        )

    monkeypatch.setattr(capture, "_write_evidence", validate)
    monkeypatch.setattr(capture, "_write_png", lambda *_args: pytest.fail("must not export PNG"))
    with pytest.raises(RuntimeError, match="failed geometry" if ready else "not ready"):
        capture._capture_frame(
            app, "editor-ready", evidence, tmp_path / "manifest", tmp_path / "png"
        )
    assert validated == ([True] if ready else [])
    assert not exported


class FakeCaptureApp:
    def __init__(self, on_pause: Any) -> None:
        self.pauses = 0
        self.on_pause = on_pause

    @asynccontextmanager
    async def run_test(self, **_kwargs: object) -> Any:
        yield self

    async def pause(self, *_args: object) -> None:
        self.pauses += 1
        self.on_pause(self.pauses)


@pytest.mark.parametrize("state_ready", [False, True])
def test_timeout_prevents_export_even_with_stale_successful_geometry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state_ready: bool
) -> None:
    evidence = tmp_path / "geometry.json"
    # A false state must defeat stale success; a true state still needs passing geometry.
    evidence.write_text(json.dumps({"ok": not state_ready}), encoding="utf-8")
    app = FakeCaptureApp(lambda _attempt: None)
    monkeypatch.setattr(capture, "create_scene", lambda *_args: app)
    monkeypatch.setattr(capture, "_scene_ready", lambda _app: state_ready)
    monkeypatch.setattr(capture, "_capture_frame", lambda *_args: pytest.fail("timeout exported"))

    def geometry(_app: object, _scene: str, fresh: Path, _manifest: Path) -> None:
        fresh.write_text(
            json.dumps(
                {"ok": not state_ready, "assertions": {"present:#required": not state_ready}}
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(capture, "_write_evidence", geometry)
    with pytest.raises(RuntimeError, match="timed out") as error:
        asyncio.run(
            capture.capture_scene("save-changes", evidence, tmp_path / "manifest", tmp_path / "png")
        )
    assert app.pauses == 100
    assert not evidence.exists()
    retained = json.loads(evidence.with_suffix(".timeout.json").read_text(encoding="utf-8"))
    assert retained["conditions"]["scene_state"] is state_ready
    assert "selector_readiness" in retained
    diagnostics = json.loads(str(error.value).split("readiness_diagnostics=", 1)[1])
    assert diagnostics["conditions"]["scene_state"] is state_ready
    assert diagnostics["geometry"]["assertions"]["present:#required"] is not state_ready


def test_state_lost_during_final_drain_blocks_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence = tmp_path / "geometry.json"
    evidence.write_text('{"ok": true}', encoding="utf-8")
    app = FakeCaptureApp(lambda _attempt: None)
    monkeypatch.setattr(capture, "create_scene", lambda *_args: app)
    monkeypatch.setattr(capture, "_scene_ready", lambda _app: app.pauses == 1)
    monkeypatch.setattr(
        capture, "_write_evidence", lambda *_args: pytest.fail("unready validation")
    )
    monkeypatch.setattr(capture, "_write_png", lambda *_args: pytest.fail("unready export"))
    with pytest.raises(RuntimeError, match="not ready"):
        asyncio.run(
            capture.capture_scene("save-changes", evidence, tmp_path / "manifest", tmp_path / "png")
        )
    assert app.pauses == 2
    assert not evidence.exists()


def test_bounded_wait_accepts_settled_geometry_and_exports_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence = tmp_path / "geometry.json"

    def settle(attempt: int) -> None:
        if attempt == 3:
            evidence.write_text('{"ok": true}', encoding="utf-8")

    app = FakeCaptureApp(settle)
    exported = []
    monkeypatch.setattr(capture, "create_scene", lambda *_args: app)
    monkeypatch.setattr(capture, "_scene_ready", lambda _app: app.pauses >= 3)
    monkeypatch.setattr(capture, "_capture_frame", lambda *_args: exported.append(True))
    asyncio.run(
        capture.capture_scene("save-changes", evidence, tmp_path / "manifest", tmp_path / "png")
    )
    assert app.pauses == 4
    assert exported == [True]


@pytest.mark.parametrize(
    ("configured", "state_ready"), [(False, True), (True, False), (True, True)]
)
def test_capture_requires_configuration_and_current_scene_state(
    monkeypatch: pytest.MonkeyPatch, configured: bool, state_ready: bool
) -> None:
    class FakeExplorer:
        creation_scene_ready = configured

        def _scene_state_ready(self) -> bool:
            return state_ready

    monkeypatch.setattr(capture, "VisualExplorerApp", FakeExplorer)
    assert capture._scene_ready(cast("Any", FakeExplorer())) == (configured and state_ready)


@pytest.mark.parametrize(
    ("configured", "state_ready", "geometry_ok"),
    [(False, True, True), (True, False, True), (True, True, False)],
)
def test_timeout_diagnostics_identify_current_failed_layer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured: bool,
    state_ready: bool,
    geometry_ok: bool,
) -> None:
    class Explorer:
        def scene_readiness_details(self) -> dict[str, Any]:
            return {
                "configured": configured,
                "conditions": {"validation_feedback": state_ready},
                "incomplete_rows": [0],
                "notice_text": "Each column needs a name and SQL type" if state_ready else "",
                "screen_class": "CreateTableScreen",
                "focus_id": "create-table-submit",
                "missing_controls": [],
            }

    monkeypatch.setattr(capture, "VisualExplorerApp", Explorer)
    evidence = tmp_path / "geometry.json"
    evidence.write_text('{"ok": true, "render_diagnostics": {}}', encoding="utf-8")

    def validate(_app: object, _scene: str, fresh: Path, _manifest: Path) -> None:
        assert fresh != evidence
        assert not fresh.exists()
        fresh.write_text(
            json.dumps({"ok": geometry_ok, "assertions": {"present:#required": geometry_ok}}),
            encoding="utf-8",
        )

    monkeypatch.setattr(capture, "_write_evidence", validate)
    result = capture._timeout_diagnostics(
        cast("Any", Explorer()), "create-table-invalid", evidence, tmp_path / "manifest"
    )
    assert result["configured"] is configured
    assert result["conditions"]["validation_feedback"] is state_ready
    assert result["geometry"]["ok"] is geometry_ok
    assert result["geometry"]["failed_assertions"] == ([] if geometry_ok else ["present:#required"])
    assert json.loads(evidence.read_text())["ok"] is True


def test_timeout_summary_is_bounded_and_reports_truncation() -> None:
    summary = capture._timeout_summary({"notice_text": "x" * 20000})
    assert len(summary) <= 8192
    assert json.loads(summary)["truncated"] is True


@pytest.mark.parametrize("error", [capture.NoMatches("gone"), capture.ScreenStackError("gone")])
def test_timeout_diagnostics_report_geometry_lifecycle_loss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
) -> None:
    monkeypatch.setattr(capture, "_scene_ready", lambda _app: False)
    monkeypatch.setattr(capture, "_write_evidence", Mock(side_effect=error))
    result = capture._timeout_diagnostics(
        cast("Any", object()), "save-changes", tmp_path / "geometry", tmp_path / "manifest"
    )
    assert result["geometry"] == {"ok": False, "lifecycle_error": type(error).__name__}


def test_timeout_diagnostics_propagate_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(capture, "_scene_ready", lambda _app: False)
    monkeypatch.setattr(capture, "_write_evidence", Mock(side_effect=RuntimeError("unexpected")))
    with pytest.raises(RuntimeError, match="unexpected"):
        capture._timeout_diagnostics(
            cast("Any", object()), "save-changes", tmp_path / "geometry", tmp_path / "manifest"
        )


def test_timeout_geometry_reports_bounded_failed_assertions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(capture, "_scene_ready", lambda _app: False)

    def validate(_app: object, _scene: str, fresh: Path, _manifest: Path) -> None:
        fresh.write_text(
            json.dumps(
                {
                    "ok": False,
                    "assertions": {f"present:#control-{index}": False for index in range(80)},
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(capture, "_write_evidence", validate)
    result = capture._timeout_diagnostics(
        cast("Any", object()), "save-changes", tmp_path / "geometry", tmp_path / "manifest"
    )
    geometry = result["geometry"]
    assert geometry["assertion_count"] == 80
    assert len(geometry["assertions"]) == len(geometry["failed_assertions"]) == 64
    assert geometry["truncated"]
    assert not geometry["ok"]


def test_timeout_retains_selector_observation_for_failed_state_and_geometry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observation = {
        "selector": {"visibility_ok": False, "findings": ["source-label:empty_content_region"]},
        "regions": [{"name": "source-label", "composed_rows": [{"text": "    "}]}],
        "collection_complete": True,
        "collection_missing": [],
        "omissions": [],
    }
    app = FakeCaptureApp(lambda _attempt: None)
    monkeypatch.setattr(capture, "_scene_ready", lambda _app: False)
    monkeypatch.setattr(capture, "selector_readiness_snapshot", lambda *_args: observation)

    def geometry(_app: object, _scene: str, fresh: Path, _manifest: Path) -> None:
        fresh.write_text(
            json.dumps({"ok": False, "assertions": {"source_selector_visible": False}}),
            encoding="utf-8",
        )

    monkeypatch.setattr(capture, "_write_evidence", geometry)
    result = capture._timeout_diagnostics(
        cast("Any", app), "create-table-schema", tmp_path / "geometry.json", tmp_path / "manifest"
    )
    assert result["conditions"] == {"scene_state": False}
    assert result["selector_readiness"] == observation
    assert result["geometry"]["ok"] is False


@pytest.mark.parametrize("scene", ["keyboard-multicursor", "find-replace", "editor-ready"])
def test_multicursor_retains_one_export_inputs_and_processing_atomically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scene: str
) -> None:
    geometry, screenshot = tmp_path / "scene.json", tmp_path / "scene.png"
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><text>界e\u0301</text></svg>'
    exports: list[str] = []
    renders: list[tuple[str, str | None]] = []
    observations: list[tuple[str, str, bytes, dict[str, Any]]] = []
    rasterization: dict[str, Any] = {"observed": "ordinary processing"}
    incomplete = {"complete": False, "missing": ["combined_svg_byte_limit"]}

    def export(**_kwargs: Any) -> str:
        exports.append(svg)
        return svg

    def write_png(raw: str, path: Path, *, framed_svg: str | None = None) -> dict[str, Any]:
        renders.append((raw, framed_svg))
        path.write_bytes(b"ordinary png")
        return rasterization

    def inputs(
        raw: str, framed: str, png: bytes, *, rasterization: dict[str, Any]
    ) -> dict[str, Any]:
        assert json.loads(geometry.read_text()) == {"ok": True}
        observations.append((raw, framed, png, rasterization))
        return incomplete

    monkeypatch.setattr(capture, "_scene_ready", lambda _app: True)
    monkeypatch.setattr(
        capture, "_write_evidence", lambda *_args: geometry.write_text('{"ok": true}')
    )
    monkeypatch.setattr(capture, "terminal_diagnostics", lambda *_args: {})
    monkeypatch.setattr(capture, "render_diagnostics", lambda *_args: {"complete": False})
    monkeypatch.setattr(capture, "_write_png", write_png)
    monkeypatch.setattr(capture, "export_input_evidence", inputs)
    monkeypatch.setattr(
        capture,
        "rasterization_comparisons",
        lambda *_args, **_kwargs: pytest.fail("multicursor must not add comparison renders"),
    )
    capture._capture_frame(
        cast("Any", SimpleNamespace(export_screenshot=export)),
        scene,
        geometry,
        tmp_path / "manifest",
        screenshot,
    )
    saved = json.loads(geometry.read_text())
    assert exports == [svg]
    assert len(renders) == 1
    assert screenshot.read_bytes() == b"ordinary png"
    if scene == "keyboard-multicursor":
        assert observations == [(svg, capture._frame_svg(svg), b"ordinary png", rasterization)]
        assert saved["export_input_evidence"] == incomplete
        assert saved["render_diagnostics"] == {"complete": False}
    else:
        assert not observations
        assert "export_input_evidence" not in saved
    assert "rasterization_comparisons" not in saved
    assert not geometry.with_suffix(".tmp").exists()


@pytest.mark.parametrize("ready", [False, True])
def test_multicursor_diagnostics_cannot_bypass_capture_gates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ready: bool
) -> None:
    geometry = tmp_path / "scene.json"
    screenshot = tmp_path / "scene.png"
    geometry.write_text('{"ok": true, "export_input_evidence": {"complete": true}}')

    def rejected(*_args: Any) -> None:
        geometry.write_text('{"ok": false, "assertions": {"required": false}}')

    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("rejected frame must not collect or export")

    monkeypatch.setattr(capture, "_scene_ready", lambda _app: ready)
    monkeypatch.setattr(capture, "_write_evidence", rejected)
    monkeypatch.setattr(capture, "terminal_diagnostics", unexpected)
    monkeypatch.setattr(capture, "export_input_evidence", unexpected)
    monkeypatch.setattr(capture, "_write_png", unexpected)
    with pytest.raises(RuntimeError, match="failed geometry" if ready else "not ready"):
        capture._capture_frame(
            cast("Any", SimpleNamespace(export_screenshot=unexpected)),
            "keyboard-multicursor",
            geometry,
            tmp_path / "manifest",
            screenshot,
        )
    assert not screenshot.exists()
    assert not geometry.with_suffix(".tmp").exists()
    if ready:
        assert json.loads(geometry.read_text())["ok"] is False
        assert "export_input_evidence" not in json.loads(geometry.read_text())
    else:
        assert not geometry.exists()
