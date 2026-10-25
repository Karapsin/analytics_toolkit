"""Retain complete scene diagnostics without importing or starting the UI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


def assert_scene_geometry(
    scene_id: str,
    geometry: dict[str, Any],
    failure_dir: Path,
) -> None:
    """Keep the strict success condition and preserve failures as pageable JSON."""
    if geometry["ok"] is True:
        return
    assertions = geometry["assertions"]
    failed = sum(value is not True for value in assertions.values())
    report = {"scene_id": scene_id, "geometry": geometry}
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    failure_dir.mkdir(parents=True, exist_ok=True)
    evidence = failure_dir / f"{scene_id}.json"
    evidence.write_text(serialized, encoding="utf-8")
    summary = (
        f"Scene {scene_id}: geometry failed; "
        f"{failed}/{len(assertions)} assertions failed. Evidence: {evidence}"
    )
    # A multiline string avoids pytest's abbreviated dictionary representation.
    message = summary[:512] + "\nComplete geometry JSON:\n" + serialized
    raise AssertionError(message)
