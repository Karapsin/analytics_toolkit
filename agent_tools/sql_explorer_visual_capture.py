"""Capture the real Textual screen headlessly on Linux, macOS, or Windows."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

from agent_tools.sql_explorer_export_observer import (
    bound_observation,
    map_export_spans,
    observe_export,
)
from agent_tools.sql_explorer_render_diagnostics import (
    BORDER_SCENES,
    SCENES,
    digest,
    render_diagnostics,
    selector_readiness_snapshot,
    terminal_diagnostics,
)
from agent_tools.sql_explorer_svg_evidence import (
    border_rasterization_comparisons,
    bound_capture_comparisons,
    caret_rasterization_comparisons,
    export_input_evidence,
    phase_rasterization_comparisons,
    rasterization_comparisons,
    rasterize_framed_svg,
    retained_stage_evidence,
)
from agent_tools.sql_explorer_svg_whitespace import restore_omitted_underlines
from agent_tools.sql_explorer_visual import TERMINAL_COLUMNS, TERMINAL_ROWS, VIEWPORT
from agent_tools.sql_explorer_visual_scene import (
    VisualExplorerApp,
    _write_evidence,
    create_scene,
)
from textual.app import ScreenStackError
from textual.css.query import NoMatches

if TYPE_CHECKING:
    from textual.app import App


_MAX_TIMEOUT_LOG_CHARS = 8192
_MAX_GEOMETRY_ASSERTIONS = 64


def _frame_svg(svg: str) -> str:
    namespace = "http://www.w3.org/2000/svg"
    frame = ET.Element(f"{{{namespace}}}svg", width=str(VIEWPORT[0]), height=str(VIEWPORT[1]))
    ET.SubElement(frame, f"{{{namespace}}}rect", width="100%", height="100%", fill="#0d1117")
    screen = ET.fromstring(svg)  # noqa: S314 - SVG is produced by our Textual app.
    screen.set("width", str(VIEWPORT[0]))
    screen.set("height", str(VIEWPORT[1]))
    screen.set("preserveAspectRatio", "xMidYMid meet")
    frame.append(screen)
    return ET.tostring(frame, encoding="unicode")


def _write_png(
    svg: str,
    destination: Path,
    *,
    framed_svg: str | None = None,
    retained_pngs: dict[str, bytes] | None = None,
    export_observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    framed = framed_svg if framed_svg is not None else _frame_svg(svg)
    original = framed
    restorations: list[dict[str, Any]] = []
    if export_observation is not None:
        framed, restorations = restore_omitted_underlines(
            framed, export_observation, raw_svg=svg
        )
    png, rasterization = (
        rasterize_framed_svg(framed)
        if retained_pngs is None
        else rasterize_framed_svg(framed, retained_pngs=retained_pngs)
    )
    rasterization["whitespace_restoration"] = {
        "conversions": restorations,
        "original_framed_svg": original,
        "original_framed_sha256": digest(original.encode("utf-8")),
        "restored_framed_svg": framed,
        "restored_framed_sha256": digest(framed.encode("utf-8")),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(png)
    return rasterization


def _geometry_ready(evidence: Path) -> bool:
    return evidence.is_file() and json.loads(evidence.read_text(encoding="utf-8"))["ok"] is True


def _scene_ready(app: App[object]) -> bool:
    return not isinstance(app, VisualExplorerApp) or (
        app.creation_scene_ready and app._scene_state_ready()  # noqa: SLF001
    )


def _observed_screenshot(
    app: App[object], scene: str, geometry: dict[str, Any], evidence: Path
) -> str:
    observation: dict[str, Any] = {}
    failed = True
    try:
        with observe_export() as observation:
            svg = app.export_screenshot(title=f"SQL Explorer — {scene}")
        try:
            map_export_spans(observation, svg)
        except (ET.ParseError, KeyError, TypeError, ValueError) as error:
            observation["complete"] = False
            observation["missing"].append("svg_mapping:" + type(error).__name__)
        failed = False
        return svg
    finally:
        geometry["export_observation"] = bound_observation(observation)
        if failed:
            _retain_export_failure(geometry, evidence)


def _retain_export_failure(geometry: dict[str, Any], evidence: Path) -> None:
    """Retain failed observations without masking the original exporter exception."""
    temporary = evidence.with_suffix(".tmp")
    try:
        temporary.write_text(
            json.dumps(geometry, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(evidence)
    except OSError:
        logging.getLogger(__name__).exception("Could not retain failed export observation")
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _capture_frame(
    app: App[object], scene: str, evidence: Path, manifest: Path, screenshot: Path
) -> None:
    evidence.unlink(missing_ok=True)
    if not _scene_ready(app):
        message = f"Scene {scene} is not ready for final capture"
        raise RuntimeError(message)
    _write_evidence(app, scene, evidence, manifest)
    geometry = json.loads(evidence.read_text(encoding="utf-8"))
    if not geometry["ok"]:
        message = f"Scene {scene} failed geometry checks: {geometry['assertions']}"
        raise RuntimeError(message)
    terminal = terminal_diagnostics(app, scene) if scene in SCENES | BORDER_SCENES else None
    if terminal is not None and "source_selector" in terminal:
        geometry["selector_readiness"] = selector_readiness_snapshot(app, scene, terminal=terminal)
    observed_scene = scene in {
        "completion-empty-columns",
        "completion-narrowed",
        "connections-searching",
    }
    if observed_scene:
        svg = _observed_screenshot(app, scene, geometry, evidence)
    else:
        svg = app.export_screenshot(title=f"SQL Explorer — {scene}")
    if terminal is None:
        _write_png(svg, screenshot)
        return
    geometry.update(
        _diagnostic_frame(
            app,
            scene,
            svg,
            terminal,
            screenshot,
            export_observation=geometry.get("export_observation"),
        )
    )
    bound_capture_comparisons(geometry)
    temporary = evidence.with_suffix(".tmp")
    temporary.write_text(json.dumps(geometry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(evidence)


def _diagnostic_frame(
    app: App[object],
    scene: str,
    svg: str,
    terminal: dict[str, Any],
    screenshot: Path,
    *,
    export_observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    geometry: dict[str, Any] = {}
    observed_scene = scene in {
        "completion-empty-columns",
        "completion-narrowed",
        "connections-searching",
    }
    framed_svg = _frame_svg(svg)
    caret_scene = scene in {"keyboard-multicursor", "editor-command-caret"}
    retain_stages = caret_scene or observed_scene or scene == "column-ruler-scrolled"
    retained: dict[str, bytes] = {}
    rasterization = (
        _write_png(
            svg,
            screenshot,
            framed_svg=framed_svg,
            retained_pngs=retained,
            export_observation=export_observation,
        )
        if retain_stages
        else _write_png(
            svg, screenshot, framed_svg=framed_svg, export_observation=export_observation
        )
    )
    png = screenshot.read_bytes()
    geometry["render_diagnostics"] = render_diagnostics(app, scene, svg, framed_svg, png, terminal)
    if scene == "database-picker":
        geometry["export_input_evidence"] = export_input_evidence(svg, framed_svg, png)
        geometry["rasterization_comparisons"] = rasterization_comparisons(
            svg, framed_svg, png, rasterization=rasterization
        )
    if scene in {
        "keyboard-multicursor",
        "editor-command-caret",
        "new-file",
        "completion-empty-columns",
        "completion-narrowed",
        "column-ruler-scrolled",
        "connections-searching",
        "result-success",
    }:
        geometry["export_input_evidence"] = export_input_evidence(
            svg, framed_svg, png, rasterization=rasterization
        )
    if caret_scene:
        geometry["rasterization_comparisons"] = caret_rasterization_comparisons(
            svg,
            framed_svg,
            png,
            rasterization=rasterization,
            intermediate_png=retained.get("intermediate"),
        )
    if scene == "connections-searching" and export_observation is not None:
        geometry["border_continuity_trial"] = border_rasterization_comparisons(
            framed_svg,
            png,
            export_observation,
            terminal,
            raw_svg=svg,
            rasterization=rasterization,
            intermediate_png=retained.get("intermediate"),
        )
    if retain_stages:
        geometry["ordinary_raster_stages"] = retained_stage_evidence(retained)
    if scene in {"column-ruler-scrolled", "keyboard-multicursor", "editor-command-caret"}:
        geometry["phase_comparisons"] = phase_rasterization_comparisons(
            framed_svg,
            png,
            rasterization=rasterization,
            intermediate_png=retained.get("intermediate"),
        )
    return geometry


def _timeout_diagnostics(
    app: App[object], scene: str, evidence: Path, manifest: Path
) -> dict[str, Any]:
    """Evaluate current failure layers without reusing stale successful evidence."""
    details: dict[str, Any] = (
        app.scene_readiness_details()
        if isinstance(app, VisualExplorerApp)
        else {"configured": True, "conditions": {"scene_state": _scene_ready(app)}}
    )
    details["selector_readiness"] = selector_readiness_snapshot(app, scene)
    details["scene_id"] = scene
    details["previous_geometry_present"] = evidence.is_file()
    try:
        # A separate, empty path forces fresh geometry while preserving its schema
        # and assertions. It cannot retain a previous successful/exported frame.
        with TemporaryDirectory(prefix="sql-visual-timeout-") as directory:
            fresh = Path(directory) / "geometry.json"
            _write_evidence(app, scene, fresh, manifest)
            geometry = json.loads(fresh.read_text(encoding="utf-8"))
        assertions = geometry["assertions"]
        entries = list(assertions.items())
        details["geometry"] = {
            "ok": geometry["ok"],
            "assertions": dict(entries[:_MAX_GEOMETRY_ASSERTIONS]),
            "failed_assertions": [key[:160] for key, value in entries if not value][
                :_MAX_GEOMETRY_ASSERTIONS
            ],
            "assertion_count": len(entries),
            "truncated": len(entries) > _MAX_GEOMETRY_ASSERTIONS,
        }
    except (NoMatches, ScreenStackError) as exc:
        details["geometry"] = {"ok": False, "lifecycle_error": type(exc).__name__}
    return details


def _timeout_summary(details: dict[str, Any]) -> str:
    """Bound logged JSON while explicitly identifying any truncated payload."""
    encoded = json.dumps(details, sort_keys=True, ensure_ascii=True)
    if len(encoded) <= _MAX_TIMEOUT_LOG_CHARS:
        return encoded
    return json.dumps({"truncated": True, "diagnostic_prefix": encoded[:2048]})


def _write_timeout_diagnostics(evidence: Path, details: dict[str, Any]) -> None:
    """Retain bounded observations outside the asynchronous capture loop."""
    timeout_path = evidence.with_suffix(".timeout.json")
    timeout_path.parent.mkdir(parents=True, exist_ok=True)
    timeout_path.write_text(json.dumps(details, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    evidence.unlink(missing_ok=True)


async def capture_scene(scene: str, evidence: Path, manifest: Path, screenshot: Path) -> None:
    app: App[object] = create_scene(scene, evidence, manifest)
    async with app.run_test(size=(TERMINAL_COLUMNS, TERMINAL_ROWS)) as pilot:
        for _attempt in range(100):
            await pilot.pause(0.1)
            if _scene_ready(app) and _geometry_ready(evidence):
                break
        else:
            details = _timeout_diagnostics(app, scene, evidence, manifest)
            _write_timeout_diagnostics(evidence, details)
            diagnostics = _timeout_summary(details)
            message = (
                f"Scene {scene} timed out waiting for readiness and geometry; "
                f"readiness_diagnostics={diagnostics}"
            )
            raise RuntimeError(message)
        # Drain queued editor/layout events, then validate the frame being exported.
        await pilot.pause()
        _capture_frame(app, scene, evidence, manifest, screenshot)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path, required=True)
    args = parser.parse_args()
    os.environ["TERM"] = "xterm-256color"
    os.environ["TEXTUAL_COLOR_SYSTEM"] = "256"
    os.environ["SQL_EXPLORER_VISUAL_REQUIRE_COLOR_256"] = "1"
    os.environ.pop("COLORTERM", None)
    asyncio.run(capture_scene(args.scene, args.evidence, args.manifest, args.screenshot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
