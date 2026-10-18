"""Use the installed Linux font while preserving other render environments."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agent_tools import sql_explorer_visual_capture as capture
from agent_tools import sql_explorer_visual_render as policy

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("platform", "present", "configured"),
    [
        ("linux", True, True),
        ("linux", False, False),
        ("darwin", True, False),
        ("win32", True, False),
    ],
)
def test_exporter_uses_shared_available_font_policy(
    tmp_path: Path,
    monkeypatch: Any,
    platform: str,
    present: bool,
    configured: bool,
) -> None:
    font = tmp_path / "DejaVuSansMono.ttf"
    if present:
        font.write_bytes(b"font boundary fixture")
    monkeypatch.setattr(policy, "Path", lambda _name: font)
    monkeypatch.setattr(policy.sys, "platform", platform)
    calls: list[dict[str, Any]] = []

    def render(**options: Any) -> bytes:
        calls.append(options)
        return b"renderer boundary PNG"

    monkeypatch.setitem(sys.modules, "resvg_py", SimpleNamespace(svg_to_bytes=render))
    destination = tmp_path / "nested/frame.png"
    capture._write_png('<svg xmlns="http://www.w3.org/2000/svg"/>', destination)
    assert destination.read_bytes() == b"renderer boundary PNG"
    assert 'width="1280"' in calls[0]["svg_string"]
    assert 'height="800"' in calls[0]["svg_string"]
    if configured:
        assert calls[0]["monospace_family"] == "DejaVu Sans Mono"
        assert calls[0]["font_files"] == [str(font)]
    else:
        assert set(calls[0]) == {"svg_string"}
