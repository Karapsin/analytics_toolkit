"""Synchronization helpers for visual-scene regression tests."""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Any
from xml.etree import ElementTree as ET


def observe_completion_open(monkeypatch: Any, app: Any) -> asyncio.Event:
    """Observe successful completion opening without changing its behavior."""
    opened = asyncio.Event()
    original = app._open_completion

    def observed(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        opened.set()
        return result

    monkeypatch.setattr(app, "_open_completion", observed)
    return opened


def configure_capture_cli(monkeypatch: Any, evidence: Any, manifest: Any, screenshot: Any) -> None:
    """Invoke the capture entrypoint through its real argument parser."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sql-explorer-visual-capture",
            "--scene",
            "editor-ready",
            "--evidence",
            str(evidence),
            "--manifest",
            str(manifest),
            "--screenshot",
            str(screenshot),
        ],
    )


def application_text_colors(svg: str) -> set[tuple[int, int, int]]:
    """Read colors used by actual terminal text, excluding window decorations."""
    root = ET.fromstring(svg)
    css = "\n".join(node.text or "" for node in root.iter() if node.tag.endswith("}style"))
    fills = {}
    for match in re.finditer(r"\.([\w-]+)\s*\{([^{}]*)\}", css):
        color = re.search(r"\bfill:\s*#([\da-fA-F]{6})\b", match[2])
        if color:
            fills[match[1]] = tuple(int(color[1][offset : offset + 2], 16) for offset in (0, 2, 4))
    matrices = [
        node
        for node in root.iter()
        if any(name.endswith("-matrix") for name in node.get("class", "").split())
    ]
    assert matrices, "The real SVG must contain terminal application text."
    return {
        fills[name]
        for matrix in matrices
        for node in matrix.iter()
        if node.tag.endswith("}text") and "".join(node.itertext()).strip()
        for name in node.get("class", "").split()
        if name in fills
    }
