from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from textual.geometry import Region

from agent_tools import sql_explorer_visual_scene as scenes

if TYPE_CHECKING:
    from pathlib import Path


def _new_file_screen(required: list[SimpleNamespace]) -> SimpleNamespace:
    dialog = SimpleNamespace(region=Region(64, 18, 80, 11))

    def query(selector: str) -> list[SimpleNamespace]:
        if selector == "#required":
            return required
        if selector == "#new-file-dialog":
            return [dialog]
        return []

    def query_one(selector: str) -> SimpleNamespace:
        if selector == "#new-file-dialog":
            return dialog
        matches = query(selector)
        if len(matches) != 1:
            message = f"Expected one widget for {selector}, got {len(matches)}"
            raise AssertionError(message)
        return matches[0]

    return SimpleNamespace(
        content_region=Region(0, 0, 208, 47),
        styles=SimpleNamespace(align_horizontal="center", align_vertical="middle"),
        query=query,
        query_one=query_one,
    )


@pytest.mark.parametrize("ready", [False, True])
def test_directory_evidence_uses_one_readiness_observation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ready: bool
) -> None:
    app = Mock(spec=scenes.VisualExplorerApp)
    app.screen_stack = [object(), object()]
    app.screen = SimpleNamespace(query=lambda _selector: [])
    app.console = SimpleNamespace(color_system="256")
    app.size = SimpleNamespace(width=208, height=47)
    observation = {
        "conditions": {"requested_focus": ready},
        "notice_text": "first observation",
    }
    app.scene_readiness_details.side_effect = [
        observation,
        {"conditions": {"requested_focus": not ready}, "notice_text": "later observation"},
    ]
    monkeypatch.setattr(scenes, "_manifest_scene", lambda *_args: {"required_elements": []})
    evidence = tmp_path / "geometry.json"
    scenes._write_evidence(app, "directory-armed", evidence, tmp_path / "manifest")
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    app.scene_readiness_details.assert_called_once_with()
    assert payload["scene_readiness"] == observation
    assert payload["assertions"]["directory:requested_focus"] is ready
    assert payload["ok"] is ready


@pytest.mark.parametrize("state", ["present", "missing", "zero_width", "zero_height", "hidden"])
def test_required_element_evidence_keeps_missing_and_hidden_controls_failing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str
) -> None:
    region = SimpleNamespace(
        x=2,
        y=3,
        width=0 if state == "zero_width" else 4,
        height=0 if state == "zero_height" else 1,
        right=6,
        bottom=4,
    )
    widget = SimpleNamespace(region=region, display=state != "hidden")
    matches = [] if state == "missing" else [widget]
    app = SimpleNamespace(
        screen=_new_file_screen(matches),
        console=SimpleNamespace(color_system="256"),
        size=SimpleNamespace(width=208, height=47),
    )
    monkeypatch.setattr(
        scenes, "_manifest_scene", lambda *_args: {"required_elements": ["#required"]}
    )
    evidence = tmp_path / "geometry.json"
    scenes._write_evidence(app, "new-file", evidence, tmp_path / "manifest")
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["assertions"]["present:#required"] is (state == "present")
    assert payload["ok"] is (state == "present")
    assert ("#required" in payload["elements"]) is (state in {"present", "hidden"})


def test_unsuccessful_refresh_preserves_passing_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = SimpleNamespace(
        screen=_new_file_screen([]),
        console=SimpleNamespace(color_system="256"),
        size=SimpleNamespace(width=208, height=47),
    )
    monkeypatch.setattr(
        scenes, "_manifest_scene", lambda *_args: {"required_elements": ["#removed"]}
    )
    evidence = tmp_path / "geometry.json"
    before = '{"ok": true, "assertions": {"present:#removed": true}}\n'
    evidence.write_text(before, encoding="utf-8")
    scenes._write_evidence(app, "new-file", evidence, tmp_path / "manifest")
    assert evidence.read_text(encoding="utf-8") == before
    assert not evidence.with_suffix(".tmp").exists()
