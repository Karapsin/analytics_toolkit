from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING

from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "changelog_controller", REPO_ROOT / ".github/agent/controller.py"
)
assert spec is not None
assert spec.loader is not None
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


def test_fold_without_fragments_preserves_current_dev_metadata_and_pending_notes(
    tmp_path: Path,
) -> None:
    (tmp_path / "agent_tools/changelog").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    files = {
        "pyproject.toml": 'version = "1.3.12.5"\n',
        "README.md": "**Version:** `1.3.12.5`\n",
        "docs/CHANGELOG.md": (
            "# Changelog\n\n## Unreleased\n\n"
            + "\n".join("- Pending note " + str(index) for index in range(10))
            + "\n  wrapped continuation\n\n"
            + "## 1.3.12.5 - 2026-10-14\n\n- Foreign release note\n"
        ),
    }
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    original = {name: (tmp_path / name).read_bytes() for name in files}

    assert not controller.fold(tmp_path)
    assert {name: (tmp_path / name).read_bytes() for name in files} == original
    assert not controller.fold(tmp_path)
    assert {name: (tmp_path / name).read_bytes() for name in files} == original
    assert not list((tmp_path / "agent_tools/changelog").glob("*.md"))
