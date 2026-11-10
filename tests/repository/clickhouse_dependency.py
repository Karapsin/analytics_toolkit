from __future__ import annotations

import pytest
from packaging.requirements import Requirement
from release_routines.lib.project_metadata import load_project

from tests._support.paths import REPO_ROOT


@pytest.mark.parametrize("python_version", ["3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14"])
def test_clickhouse_driver_range_preserves_legacy_python_and_fixed_modern_accelerator(
    python_version: str,
) -> None:
    project = load_project(REPO_ROOT / "pyproject.toml")
    requirements = [Requirement(item) for item in project["dependencies"]]
    selected = [
        requirement
        for requirement in requirements
        if requirement.name == "clickhouse-connect"
        and (
            requirement.marker is None
            or requirement.marker.evaluate({"python_version": python_version})
        )
    ]
    assert len(selected) == 1
    specifier = selected[0].specifier
    modern = tuple(int(part) for part in python_version.split(".")) >= (3, 13)
    assert specifier.contains("1.4.0") is modern
    assert specifier.contains("0.5.14") is not modern
    assert specifier.contains("1.3.0") is False
    assert specifier.contains("2.0.0") is False
