"""The fixed-color capture must not inherit a terminal's monochrome preference."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from agent_tools import mcp_server, sql_explorer_visual
from agent_tools import sql_explorer_visual_capture as capture
from tests.agent_tools._support.visual import application_text_colors, configure_capture_cli

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("no_color", [None, "", "1"])
def test_capture_cli_preserves_application_palette(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_color: str | None
) -> None:
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("TEXTUAL_COLOR_SYSTEM", "256")
    monkeypatch.setenv("SQL_EXPLORER_VISUAL_REQUIRE_COLOR_256", "0")
    monkeypatch.setenv("COLORTERM", "truecolor")
    if no_color is None:
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", no_color)
    rendered: list[tuple[str, Path]] = []
    monkeypatch.setattr(capture, "_write_png", lambda svg, path: rendered.append((svg, path)))
    evidence = tmp_path / "geometry.json"
    screenshot = tmp_path / "scene.png"
    configure_capture_cli(
        monkeypatch, evidence, mcp_server.REPO_ROOT / sql_explorer_visual.MANIFEST, screenshot
    )

    assert capture.main() == 0

    assert json.loads(evidence.read_text())["ok"] is True
    assert len(rendered) == 1
    assert rendered[0][1] == screenshot
    colors = application_text_colors(rendered[0][0])
    # Assert the app's amber accents and blue SQL syntax, not Rich's colored chrome.
    assert any(red >= green + 40 and green >= blue + 20 for red, green, blue in colors), colors
    assert any(blue >= red + 20 and blue >= green >= red for red, green, blue in colors), colors
