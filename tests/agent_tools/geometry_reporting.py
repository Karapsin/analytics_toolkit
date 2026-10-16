"""Fast scene-reporting regressions; no rendering or external services."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from tests.agent_tools._support.geometry import assert_scene_geometry

if TYPE_CHECKING:
    from pathlib import Path


def test_failed_geometry_keeps_every_assertion_and_nested_coordinate(tmp_path: Path) -> None:
    geometry: dict[str, Any] = {
        "ok": False,
        "assertions": {
            **{f"present:element-{index}": True for index in range(100)},
            "popup_fits": False,
            "highlight_readable": False,
        },
        "elements": {
            "#completion-menu": {
                "x": 21,
                "y": 4,
                "width": 32,
                "height": 6,
                "children": [{"name": "customer_four", "region": {"right": 53}}],
            },
        },
        "screen": {"width": 208, "height": 47},
    }
    original = json.dumps(geometry, sort_keys=True)
    destination = tmp_path / "failures"
    with pytest.raises(AssertionError, match="Scene completion") as caught:
        assert_scene_geometry("completion", geometry, destination)
    summary, serialized = str(caught.value).split("\nComplete geometry JSON:\n", 1)
    assert len(summary) <= 512
    assert "Scene completion" in summary
    assert "2/102 assertions failed" in summary
    expected = {"scene_id": "completion", "geometry": geometry}
    assert json.loads(serialized) == expected
    retained = destination / "completion.json"
    assert retained.read_text(encoding="utf-8") == serialized
    assert json.loads(retained.read_text(encoding="utf-8")) == expected
    assert len(serialized.splitlines()) > 100
    assert json.dumps(geometry, sort_keys=True) == original


@pytest.mark.parametrize("ok", [False, 1, "true", None])
def test_geometry_requires_literal_true(tmp_path: Path, ok: Any) -> None:
    with pytest.raises(AssertionError, match="Scene completion"):
        assert_scene_geometry("completion", {"ok": ok, "assertions": {}}, tmp_path)


def test_successful_geometry_creates_no_failure_evidence(tmp_path: Path) -> None:
    destination = tmp_path / "failures"
    assert_scene_geometry("completion", {"ok": True, "assertions": {}}, destination)
    assert not destination.exists()
