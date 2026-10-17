from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING

import pytest

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


@pytest.mark.parametrize(
    "body",
    [
        "- Existing entry",
        "- Existing entry\n- Following entry",
        "- Existing entry\n\nA retained paragraph.",
    ],
)
def test_fold_consumes_already_represented_fragment_without_changing_bytes(
    tmp_path: Path, body: str
) -> None:
    fragments = tmp_path / "agent_tools/changelog"
    fragments.mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    changelog = tmp_path / "docs/CHANGELOG.md"
    original = "# Changelog\n\n## Unreleased\n\n" + body + "\n\n## 1.0\n\n- History\n"
    changelog.write_text(original, encoding="utf-8")
    fragment = fragments / "task.md"
    for _ in range(2):  # Recreated fragment after an accepted publication/lost response.
        fragment.write_text("- Existing entry\n", encoding="utf-8")
        assert controller.fold(tmp_path)
        assert not fragment.exists()
        assert changelog.read_text(encoding="utf-8") == original
    assert not controller.fold(tmp_path)


@pytest.mark.parametrize(
    "continuation", ["  continuation", "unindented continuation", "\n  continuation"]
)
def test_fold_deduplicates_fragments_only_within_unreleased(
    tmp_path: Path, continuation: str
) -> None:
    fragments = tmp_path / "agent_tools/changelog"
    fragments.mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    changelog = tmp_path / "docs/CHANGELOG.md"
    retained = "- Wrapped entry\n" + continuation + "\n\nA retained paragraph."
    historical = "## 1.0\n\n- Historical entry\n"
    changelog.write_text(
        "# Changelog\n\n## Unreleased\n\n" + retained + "\n\n" + historical,
        encoding="utf-8",
    )
    summaries = ["- Historical entry", "- Historical entry", "- Distinct entry", "- Wrapped entry"]
    for index, summary in enumerate(summaries):
        (fragments / (str(index) + ".md")).write_text(summary + "\n", encoding="utf-8")
    version = tmp_path / "pyproject.toml"
    version.write_text('version = "1.3.12.5"\n', encoding="utf-8")
    assert controller.fold(tmp_path)
    expected = (
        "# Changelog\n\n## Unreleased\n\n"
        + retained
        + "\n- Historical entry\n- Distinct entry\n- Wrapped entry\n\n"
        + historical
    )
    assert changelog.read_text(encoding="utf-8") == expected
    assert not list(fragments.glob("*.md"))
    assert version.read_text(encoding="utf-8") == 'version = "1.3.12.5"\n'
    assert not controller.fold(tmp_path)
    assert changelog.read_text(encoding="utf-8") == expected


def test_fold_validates_every_fragment_before_consuming_any(tmp_path: Path) -> None:
    fragments = tmp_path / "agent_tools/changelog"
    fragments.mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    changelog = tmp_path / "docs/CHANGELOG.md"
    original = "# Changelog\n\n## Unreleased\n\n- Existing entry\n"
    changelog.write_text(original, encoding="utf-8")
    (fragments / "first.md").write_text("- Existing entry\n", encoding="utf-8")
    (fragments / "second.md").write_text("- Invalid\n  continuation\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="one concise bullet"):
        controller.fold(tmp_path)
    assert changelog.read_text(encoding="utf-8") == original
    assert len(list(fragments.glob("*.md"))) == 2
