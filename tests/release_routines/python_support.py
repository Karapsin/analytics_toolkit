from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from release_routines.lib import python_support as support
from release_routines.lib.python_support_install import installation
from release_routines.lib.python_support_test import test_arguments as arguments_for_tests

from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path


def policy() -> dict:
    return support.load_policy(REPO_ROOT)


def test_overlap_retains_floor_through_multiple_releases_then_shrinks() -> None:
    current = policy()
    expanded = support.transition(current, "3.15", {"tui"})
    assert expanded["minimum"] == "3.9"
    assert expanded["maximum"] == "3.15"
    assert expanded["extras"]["tui"]["maximum"] == "3.15"
    assert expanded["extras"]["airflow"]["maximum"] == "3.14"
    expanded = support.transition(expanded, "3.17", set())
    assert len(support.versions(expanded)) == 9
    assert expanded["minimum"] == "3.9"
    shrunk = support.transition(expanded, "3.17", {*support.EXTRAS, "all"})
    assert shrunk["minimum"] == "3.11"
    assert len(support.versions(shrunk)) == 7


def test_individual_extra_readiness_does_not_replace_all_installation() -> None:
    expanded = support.transition(policy(), "3.17", set(support.EXTRAS))
    assert expanded["minimum"] == "3.9"


def test_all_range_is_intersection_and_can_be_empty() -> None:
    current = policy()
    assert support.extra_range(current, "all") == (10, 14)
    assert not support.supports_extra(current, "all", "3.9")
    current["extras"]["airflow"]["maximum"] = "3.9"
    assert support.extra_range(current, "all") == (10, 9)
    assert not any(row["extra"] == "all" for row in support.extra_matrix(current))


def test_extra_support_is_clipped_to_core_after_shrinking() -> None:
    shrunk = support.transition(policy(), "3.17", {*support.EXTRAS, "all"})
    assert not support.supports_extra(shrunk, "airflow", "3.9")
    assert not support.supports_extra(shrunk, "all", "3.10")
    assert support.extra_range(shrunk, "all") == (11, 17)


@pytest.mark.parametrize(("latest", "ready"), [("3.13", set()), ("3.15", {"unknown"})])
def test_invalid_transitions_fail_closed(latest: str, ready: set[str]) -> None:
    with pytest.raises(ValueError, match="cannot move backwards"):
        support.transition(policy(), latest, ready)


def test_python38_cannot_be_reintroduced() -> None:
    current = policy()
    current["minimum"] = "3.8"
    with pytest.raises(ValueError, match=r"3\.8"):
        support.validate_policy(current)


def test_release_discovery_ignores_prereleases_unpublished_and_patch_duplicates() -> None:
    releases = [
        {"name": "Python 3.14.0", "is_published": True, "pre_release": False},
        {"name": "Python 3.14.5", "is_published": True, "pre_release": False},
        {"name": "Python 3.15.0rc1", "is_published": True, "pre_release": True},
        {"name": "Python 3.16.0", "is_published": False, "pre_release": False},
    ]
    assert support.stable_minors(releases) == ["3.14"]
    with pytest.raises(ValueError, match="No published"):
        support.stable_minors([])


def test_policy_drives_interpreters_minimum_and_sdk_environments() -> None:
    current = policy()
    tox = support.render_tox(current)
    matrix = support.test_matrix(current)
    assert matrix[-1] == {"python-version": "3.9", "env_name": "py39-min"}
    assert support.coverage_python(current) == "3.12"
    assert support.sdk_python(current) == "3.10"
    assert "py38" not in tox
    assert "constraints/python-min.txt" in tox
    shrunk = support.transition(current, "3.17", {*support.EXTRAS, "all"})
    assert support.sdk_python(shrunk) == "3.11"
    assert "py311-sdk31" in support.render_tox(shrunk)


def test_generated_metadata_is_current_and_drift_is_rejected(tmp_path: Path) -> None:
    current = policy()
    support.sync(REPO_ROOT, current, check=True)
    for path in ("pyproject.toml", "README.md", "tox.ini"):
        (tmp_path / path).write_text((REPO_ROOT / path).read_text())
    project = tmp_path / "pyproject.toml"
    project.write_text(
        project.read_text().replace('requires-python = ">=3.9', 'requires-python = ">=3.8')
    )
    with pytest.raises(ValueError, match="metadata drift"):
        support.sync(tmp_path, current, check=True)
    support.sync(tmp_path, current)
    support.sync(tmp_path, current, check=True)


def test_installation_and_test_selection_follow_extra_ranges(tmp_path: Path) -> None:
    (tmp_path / "release_routines").mkdir()
    current = support.transition(policy(), "3.15", set())
    (tmp_path / support.POLICY_PATH).write_text(json.dumps(current))
    assert "datalens" not in installation(tmp_path, "3.9", "latest")[-1]
    assert "constraints/python-min.txt" in installation(tmp_path, "3.9", "min")
    assert "--ignore=tests/sql/explorer" in arguments_for_tests(tmp_path, "3.15")


def test_airflow_integration_matrix_preserves_both_majors_and_transports() -> None:
    workflow = (REPO_ROOT / ".github/workflows/sql-integration.yml").read_text()
    assert "airflow: [2, 3]" in workflow
    assert "requirements-auth-airflow3.txt" in workflow
    manifest = json.loads((REPO_ROOT / "integration/sql_coverage_manifest.json").read_text())
    assert manifest["scenarios"]["auth.airflow.variables"]["profile"] == "auth"
    assert "HTTP + native, Airflow" in workflow
