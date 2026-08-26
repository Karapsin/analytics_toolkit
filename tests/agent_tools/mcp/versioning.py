from __future__ import annotations

from tests.agent_tools._support.mcp import (
    Path,
    _write_minimal_repo_files,
    _write_unreleased_changelog,
    mcp_server,
    pytest,
)


def test_dependency_metadata_status_detects_malformed_optional_extra(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    readme = (root / "README.md").read_text(encoding="utf-8")
    (root / "README.md").write_text(
        readme.replace("; optional extra `airflow`", ""),
        encoding="utf-8",
    )

    result = mcp_server.dependency_metadata_status(root=str(root))

    assert result["ok"] is False
    assert "Unsupported README Suggests entry" in result["blockers"][0]["message"]


def test_dependency_metadata_status_detects_readme_constraint_mismatch(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    readme = (root / "README.md").read_text(encoding="utf-8")
    (root / "README.md").write_text(
        readme.replace(
            "[requests](https://pypi.org/project/requests/) (`>=2.28.2,<3`)",
            "[requests](https://pypi.org/project/requests/) (`>=2.28.1,<3`)",
        ),
        encoding="utf-8",
    )

    result = mcp_server.dependency_metadata_status(root=str(root))

    assert result["ok"] is False
    assert "README Imports" in result["blockers"][0]["message"]
    assert "do not match pyproject" in result["blockers"][0]["message"]


def test_increment_version_carries_four_part_versions() -> None:
    assert mcp_server._increment_version("1.3.6.6") == "1.3.6.7"
    assert mcp_server._increment_version("1.3.6.19") == "1.3.7.0"
    assert mcp_server._increment_version("1.3.19.19") == "1.4.0.0"
    assert mcp_server._increment_version("1.19.19.19") == "2.0.0.0"


@pytest.mark.parametrize("version", ["1.2.3", "1.2.3.20", "1.2.-1.0"])
def test_increment_version_rejects_invalid_versions(version: str) -> None:
    with pytest.raises(ValueError):
        mcp_server._increment_version(version)


def test_version_bump_does_not_partially_write_when_release_changelog_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project", version="1.3.9.13")
    _write_unreleased_changelog(root, [f"Existing change {index}" for index in range(1, 10)])
    original_pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    original_readme = (root / "README.md").read_text(encoding="utf-8")

    def fail_changelog(text: str, entry: str) -> str:
        msg = "Could not update changelog"
        raise ValueError(msg)

    monkeypatch.setattr(mcp_server, "_release_unreleased_changelog_text", fail_changelog)

    result = mcp_server.version_bump("Consolidated agent MCP workflow", root=str(root))

    assert result["ok"] is False
    assert (root / "pyproject.toml").read_text(encoding="utf-8") == original_pyproject
    assert (root / "README.md").read_text(encoding="utf-8") == original_readme


def test_version_bump_fails_when_readme_version_marker_is_missing_at_threshold(
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project", version="1.3.9.13")
    _write_unreleased_changelog(root, [f"Existing change {index}" for index in range(1, 10)])
    (root / "README.md").write_text("# analytics_toolkit\n", encoding="utf-8")

    result = mcp_server.version_bump("Consolidated agent MCP workflow", root=str(root))

    assert result["ok"] is False
    assert result["blockers"][0]["message"] == "Could not update README version"
    assert 'version = "1.3.9.13"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "## 1.3.9.14 - " not in (root / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")


def test_version_bump_force_release_requires_unreleased_entries(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")

    result = mcp_server.version_bump(
        change_type="release",
        force_release=True,
        root=str(root),
    )

    assert result["ok"] is False
    assert result["blockers"][0]["message"] == (
        "no unreleased changelog entries are available to release"
    )


def test_version_bump_force_releases_below_threshold(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project", version="1.3.9.13")
    _write_unreleased_changelog(root, ["Existing change 1", "Existing change 2"])
    changelog_path = root / "docs" / "CHANGELOG.md"
    changelog_path.write_text(
        changelog_path.read_text(encoding="utf-8").replace(
            "- Existing change 1.",
            "- Existing change 1 with wrapped\n  continuation text.",
        ),
        encoding="utf-8",
    )

    dry_run = mcp_server.version_bump(
        change_type="release",
        force_release=True,
        root=str(root),
        dry_run=True,
    )
    applied = mcp_server.version_bump(
        change_type="release",
        force_release=True,
        root=str(root),
    )

    assert dry_run["result"]["decision"] == "bump"
    assert dry_run["result"]["planned_version"] == "1.3.9.14"
    assert dry_run["result"]["unreleased_count"] == 2
    assert applied["result"]["decision"] == "bump"
    assert 'version = "1.3.9.14"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    changelog = (root / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## Unreleased" not in changelog
    assert "- Existing change 1 with wrapped\n  continuation text." in changelog
    assert "- Existing change 2." in changelog


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"change_type": "implementation", "force_release": True},
            "force_release requires a release-oriented change_type",
        ),
        (
            {"summary": "Release summary", "change_type": "release", "force_release": True},
            "omit summary when force_release is enabled",
        ),
    ],
)
def test_version_bump_rejects_invalid_force_release_options(
    tmp_path: Path,
    kwargs: dict[str, object],
    message: str,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    _write_unreleased_changelog(root, ["Existing change"])

    result = mcp_server.version_bump(root=str(root), **kwargs)

    assert result["ok"] is False
    assert result["blockers"][0]["message"] == message


def test_version_bump_releases_tenth_unreleased_bullet(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project", version="1.3.9.13")
    _write_unreleased_changelog(root, [f"Existing change {index}" for index in range(1, 10)])
    changelog_path = root / "docs" / "CHANGELOG.md"
    changelog_path.write_text(
        changelog_path.read_text(encoding="utf-8").replace(
            "- Existing change 1.",
            "- Existing change 1 with wrapped\n  continuation text.",
        ),
        encoding="utf-8",
    )

    assert (
        mcp_server._count_unreleased_changelog_bullets(changelog_path.read_text(encoding="utf-8"))
        == 9
    )

    dry_run = mcp_server.version_bump("Tenth change", root=str(root), dry_run=True)
    applied = mcp_server.version_bump("Tenth change", root=str(root))

    assert dry_run["result"]["decision"] == "bump"
    assert dry_run["result"]["planned_version"] == "1.3.9.14"
    assert 'version = "1.3.9.14"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "**Version:** `1.3.9.14`" in (root / "README.md").read_text(encoding="utf-8")
    changelog = (root / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## Unreleased" not in changelog
    assert "## 1.3.9.14 - " in changelog
    assert "- Existing change 1 with wrapped\n  continuation text." in changelog
    assert "- Tenth change." in changelog
    assert applied["ok"] is True


def test_version_bump_skips_documentation_only_changes(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")

    result = mcp_server.version_bump(
        "Updated docs",
        change_type="documentation",
        root=str(root),
    )

    assert result["result"]["decision"] == "no_bump"
    assert 'version = "1.3.9.13"' in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_version_bump_updates_unreleased_below_threshold(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project", version="1.3.9.13")

    dry_run = mcp_server.version_bump(
        "Consolidated agent MCP workflow", root=str(root), dry_run=True
    )
    applied = mcp_server.version_bump("Consolidated agent MCP workflow", root=str(root))

    assert dry_run["result"]["decision"] == "unreleased"
    assert dry_run["result"]["planned_version"] is None
    assert applied["result"]["decision"] == "unreleased"
    assert 'version = "1.3.9.13"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "**Version:** `1.3.9.13`" in (root / "README.md").read_text(encoding="utf-8")
    changelog = (root / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## Unreleased" in changelog
    assert "- Consolidated agent MCP workflow." in changelog
    assert "## 1.3.9.14 - " not in changelog
    assert applied["ok"] is True
