"""Capture the real Textual screen headlessly on Linux, macOS, or Windows."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

from agent_tools.sql_explorer_render_diagnostics import (
    SCENES,
    render_diagnostics,
    selector_readiness_snapshot,
    terminal_diagnostics,
)
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


def _write_png(svg: str, destination: Path, *, framed_svg: str | None = None) -> None:
    import resvg_py  # noqa: PLC0415 - optional agent capture dependency.

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(
        resvg_py.svg_to_bytes(svg_string=framed_svg if framed_svg is not None else _frame_svg(svg))
    )


def _geometry_ready(evidence: Path) -> bool:
    return evidence.is_file() and json.loads(evidence.read_text(encoding="utf-8"))["ok"] is True


def _scene_ready(app: App[object]) -> bool:
    return not isinstance(app, VisualExplorerApp) or (
        app.creation_scene_ready and app._scene_state_ready()  # noqa: SLF001
    )


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
    terminal = terminal_diagnostics(app, scene) if scene in SCENES else None
    if terminal is not None and "source_selector" in terminal:
        geometry["selector_readiness"] = selector_readiness_snapshot(app, scene, terminal=terminal)
    svg = app.export_screenshot(title=f"SQL Explorer — {scene}")
    if terminal is None:
        _write_png(svg, screenshot)
        return
    framed_svg = _frame_svg(svg)
    _write_png(svg, screenshot, framed_svg=framed_svg)
    geometry["render_diagnostics"] = render_diagnostics(
        app, scene, svg, framed_svg, screenshot.read_bytes(), terminal
    )
    temporary = evidence.with_suffix(".tmp")
    temporary.write_text(json.dumps(geometry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(evidence)


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
