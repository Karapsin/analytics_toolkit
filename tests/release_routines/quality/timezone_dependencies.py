"""Portable IANA timezone support across the public Python/platform window."""

from __future__ import annotations

import pytest
from packaging.requirements import Requirement
from release_routines.lib.check_minimum_constraints import validate_minimum_constraints
from release_routines.lib.check_readme_dependencies import validate_readme_dependencies
from release_routines.lib.project_metadata import load_project

from tests._support.paths import REPO_ROOT


@pytest.mark.parametrize(
    ("python_version", "needs_backport"), [("3.8", True), ("3.9", False), ("3.14", False)]
)
@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_zoneinfo_backport_only_installs_before_python39_and_iana_data_is_portable(
    python_version: str, needs_backport: bool, platform: str
) -> None:
    project = load_project(REPO_ROOT / "pyproject.toml")
    requirements = {
        requirement.name: requirement for requirement in map(Requirement, project["dependencies"])
    }
    backport = requirements["backports.zoneinfo"]
    assert backport.marker is not None
    assert (
        backport.marker.evaluate({"python_version": python_version, "sys_platform": platform})
        is needs_backport
    )
    assert "0.2.1" in backport.specifier
    assert "0.3" not in backport.specifier
    data = requirements["tzdata"]
    assert data.marker is None
    assert "2022.7" in data.specifier
    assert "2022.6" not in data.specifier


def test_timezone_runtime_markers_and_floors_match_all_constraint_and_readme_branches() -> None:
    project = load_project(REPO_ROOT / "pyproject.toml")
    constraints = (REPO_ROOT / "constraints/py39-min.txt").read_text(encoding="utf-8")
    assert validate_minimum_constraints(project["dependencies"], constraints) == []
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert validate_readme_dependencies(project, readme) == []
