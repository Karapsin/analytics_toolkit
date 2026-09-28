"""Capture the real Textual screen headlessly on Linux, macOS, or Windows."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from agent_tools.sql_explorer_visual import TERMINAL_COLUMNS, TERMINAL_ROWS, VIEWPORT
from agent_tools.sql_explorer_visual_scene import _write_evidence, create_scene

if TYPE_CHECKING:
    from textual.app import App


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


def _write_png(svg: str, destination: Path) -> None:
    import resvg_py  # noqa: PLC0415 - optional agent capture dependency.

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(resvg_py.svg_to_bytes(svg_string=_frame_svg(svg)))


def _geometry_ready(evidence: Path) -> bool:
    return evidence.is_file() and json.loads(evidence.read_text(encoding="utf-8"))["ok"] is True


def _capture_frame(
    app: App[object], scene: str, evidence: Path, manifest: Path, screenshot: Path
) -> None:
    evidence.unlink(missing_ok=True)
    _write_evidence(app, scene, evidence, manifest)
    geometry = json.loads(evidence.read_text(encoding="utf-8"))
    if not geometry["ok"]:
        message = f"Scene {scene} failed geometry checks: {geometry['assertions']}"
        raise RuntimeError(message)
    _write_png(app.export_screenshot(title=f"SQL Explorer — {scene}"), screenshot)


async def capture_scene(scene: str, evidence: Path, manifest: Path, screenshot: Path) -> None:
    app: App[object] = create_scene(scene, evidence, manifest)
    async with app.run_test(size=(TERMINAL_COLUMNS, TERMINAL_ROWS)) as pilot:
        for _attempt in range(100):
            await pilot.pause(0.1)
            if _geometry_ready(evidence):
                break
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
