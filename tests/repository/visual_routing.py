"""Service-free routing guards; capture identity remains a separate gate."""

from __future__ import annotations

from typing import Any

import pytest

from tests.repository._support.publication import (
    control as control,  # noqa: PLC0414 - pytest fixture
)
from tests.repository._support.publication import (
    publication as publication,  # noqa: PLC0414 - pytest fixture
)
from tests.repository.github_agent import controller

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
def test_pi_publication_has_no_visual_review_gate(
    publication: Any, path: str, direction: str
) -> None:
    """The Pi runs no PNG review; fraud check and fast CI gate the published head."""
    work, git, commit, task, artifact, gh, pushed, _history = publication
    source, target = ("docs/renamed.md", path) if direction == "in" else (path, "docs/renamed.md")
    for name in (source, target):
        (work / name).parent.mkdir(parents=True, exist_ok=True)
    (work / source).write_text("Retained harness content.\n", encoding="utf-8")
    head = commit()
    git("mv", source, target)
    value = task(head, head)
    output = artifact(value)
    assert not (output / "visual-receipt.json").exists()
    controller.publish(gh, value, output, work)
    assert pushed


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
