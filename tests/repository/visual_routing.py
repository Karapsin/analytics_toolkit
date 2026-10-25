"""Service-free routing guards; capture identity remains a separate gate."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from tests.repository._support.publication import (
    control as control,  # noqa: PLC0414 - pytest fixture
)
from tests.repository._support.publication import (
    publication as publication,  # noqa: PLC0414 - pytest fixture
)
from tests.repository.github_agent import controller

if TYPE_CHECKING:
    from pathlib import Path

HARNESS_PATHS = (
    "tests/agent_tools/visual.py",
    "tests/agent_tools/_support/visual.py",
    "tests/agent_tools/_support/geometry.py",
    "tests/agent_tools/visual_diagnostics.py",
    "tests/agent_tools/geometry_reporting.py",
)
VISUAL_PATHS = (
    "analytics_toolkit/sql_explorer/editor.py",
    "agent_tools/sql_explorer_visual.py",
    "agent_tools/sql_explorer_visual_capture.py",
    "visual-tests/sql_explorer/manifest.json",
    "tests/sql_explorer/completion.py",
    *HARNESS_PATHS,
)


@pytest.mark.parametrize("path", VISUAL_PATHS)
def test_visual_path_families_require_review(path: str) -> None:
    assert controller.visual_required([path])
    assert controller.visual_required(["docs/notes.md", path])


@pytest.mark.parametrize("path", HARNESS_PATHS)
@pytest.mark.parametrize("direction", ["in", "out"])
def test_actual_publication_routes_both_rename_endpoints(
    publication: Any, monkeypatch: Any, path: str, direction: str
) -> None:
    work, git, commit, task, artifact, gh, pushed, _history = publication
    source, target = ("docs/renamed.md", path) if direction == "in" else (path, "docs/renamed.md")
    for name in (source, target):
        (work / name).parent.mkdir(parents=True, exist_ok=True)
    (work / source).write_text("Retained harness content.\n", encoding="utf-8")
    head = commit()
    git("mv", source, target)
    value = task(head, head)
    output = artifact(value)
    error = RuntimeError("Fresh complete visual review required.")
    visual = Mock(side_effect=error)
    monkeypatch.setattr(controller, "verify_visual", visual)
    with pytest.raises(RuntimeError, match="Fresh complete visual review"):
        controller.publish(gh, value, output, work)
    visual.assert_called_once_with(output, output / "fix.patch", value)
    assert pushed == []


@pytest.mark.parametrize(
    "path",
    [
        "tests/agent_tools/task_context.py",
        "tests/agent_tools/visual.py.backup",
        "tests/agent_tools/_support/geometry.py.old",
        "tests/agent_tools/geometry_reporting.pyi",
        "docs/visual.py",
        "analytics_toolkit/sql/backends/ch/adapter.py",
    ],
)
def test_unrelated_paths_do_not_require_visual_review(path: str) -> None:
    assert not controller.visual_required([path])
    assert not controller.visual_required([])


def test_harness_routing_does_not_relax_capture_identity(tmp_path: Path) -> None:
    patch = tmp_path / "fix.patch"
    patch.write_bytes(b"geometry diagnostic repair")
    task = {"head": "a" * 40, "task_digest": "b" * 64}
    receipt = {
        "task": task,
        "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
        "capture": {
            "capture": {"status": "pass", "workspace_removed": True},
            "scenes": {"completion": {}},
        },
        "scenes": {"completion": {"verdict": "pass", "notes": "All rows visible"}},
    }
    target = tmp_path / "visual-receipt.json"
    target.write_text(json.dumps(receipt), encoding="utf-8")
    controller.verify_visual(tmp_path, patch, task)
    for change in (
        {"task": {**task, "head": "c" * 40}},
        {"patch_sha256": "d" * 64},
        {"scenes": {}},
        {"scenes": {"completion": {"verdict": "fail", "notes": "Clipped"}}},
        {"capture": {"capture": {"status": "fail", "workspace_removed": True}}},
        {"capture": {"capture": {"status": "pass", "workspace_removed": False}}},
    ):
        changed = copy.deepcopy(receipt)
        changed.update(change)
        target.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(
            RuntimeError,
            match=r"Stale visual|Partial visual|Non-green visual|Incomplete visual",
        ):
            controller.verify_visual(tmp_path, patch, task)
