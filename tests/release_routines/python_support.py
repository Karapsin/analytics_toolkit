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
    return {
        "normal_window": 7,
        "minimum": "3.9",
        "maximum": "3.15",
        "extras": {
            extra: {"minimum": "3.10" if extra == "datalens" else "3.9", "maximum": "3.14"}
            for extra in support.EXTRAS
        },
    }


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
    current = support.load_policy(REPO_ROOT)
    support.sync(REPO_ROOT, current, check=True)
    for path in ("pyproject.toml", "README.md", "tox.ini"):
        (tmp_path / path).write_text((REPO_ROOT / path).read_text())
    project = tmp_path / "pyproject.toml"
    project.write_text(
        project.read_text().replace(
            f'requires-python = ">={current["minimum"]},<3.{support.minor(current["maximum"]) + 1}"',
            'requires-python = ">=3.8"',
        )
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
    assert "fromJSON(needs.policy.outputs.auth)" in workflow
    selector = (REPO_ROOT / "release_routines/lib/python_support_integration.py").read_text()
    assert "integration/requirements-auth-airflow3.txt" in selector
    manifest = json.loads((REPO_ROOT / "integration/sql_coverage_manifest.json").read_text())
    assert manifest["scenarios"]["auth.airflow.variables"]["profile"] == "auth"
    assert "HTTP + native, Airflow" in workflow


@pytest.mark.parametrize(
    "ready",
    [set(), {"tui"}, set(support.EXTRAS), {*support.EXTRAS, "all"}],
)
def test_generated_proposals_validate_without_live_fixture_drift(
    tmp_path: Path, ready: set[str]
) -> None:
    (tmp_path / "release_routines").mkdir()
    (tmp_path / "docs").mkdir()
    for path in ("pyproject.toml", "README.md", "tox.ini", "docs/PYTHON_SUPPORT.md"):
        (tmp_path / path).write_text((REPO_ROOT / path).read_text())
    proposal = support.transition(policy(), "3.16", ready)
    (tmp_path / support.POLICY_PATH).write_text(json.dumps(proposal))
    support.sync(tmp_path, proposal)
    support.sync(tmp_path, support.load_policy(tmp_path), check=True)
    requirement = f'>={proposal["minimum"]},<3.17'
    assert requirement in (tmp_path / "pyproject.toml").read_text()
    assert requirement in (tmp_path / "README.md").read_text()
    assert support.render_tox(proposal) == (tmp_path / "tox.ini").read_text()
    expected_floor = "3.10" if ready == {*support.EXTRAS, "all"} else "3.9"
    assert support.test_matrix(proposal)[-1]["python-version"] == expected_floor
    assert all(
        support.supports_extra(proposal, row["extra"], row["python-version"])
        for row in support.extra_matrix(proposal)
    )


def test_live_policy_invariants_follow_the_generated_window() -> None:
    current = support.load_policy(REPO_ROOT)
    matrix_versions = [row["python-version"] for row in support.test_matrix(current)[:-1]]
    assert matrix_versions == support.versions(current)
    assert support.test_matrix(current)[-1]["env_name"] == support.env_name(current["minimum"], "min")
    assert all(
        support.supports_extra(current, row["extra"], row["python-version"])
        for row in support.extra_matrix(current)
    )


def test_airflow_endpoints_follow_floor_and_report_retired_majors() -> None:
    shrunk = support.transition(policy(), "3.16", {*support.EXTRAS, "all"})
    rows = support.airflow_matrix(shrunk)
    assert {"2", "3"} == {row["airflow"].split(".")[0] for row in rows}
    assert all(support.minor(row["python"]) >= 10 for row in rows)
    assert {"airflow": "2.4.3", "python": "3.10"} in rows
    assert len({(row["airflow"], row["python"]) for row in rows}) == len(rows)
    later = support.transition(shrunk, "3.19", {*support.EXTRAS, "all"})
    assert support.retired_airflow_majors(later) == ["2"]
    assert all(row["airflow"].startswith("3.") for row in support.airflow_matrix(later))


def test_coverage_clamps_midpoint_into_common_extra_range() -> None:
    current = support.transition(policy(), "3.23", set())
    current["extras"]["tui"]["maximum"] = "3.11"
    current["extras"]["datalens"]["maximum"] = "3.12"
    assert support.coverage_python(current) == "3.11"
    assert support.coverage_versions(current) == ["3.11"]


def test_disjoint_coverage_selections_cover_all_suites_and_reject_missing_extras(
    tmp_path: Path,
) -> None:
    current = policy()
    current["extras"]["tui"]["maximum"] = "3.9"
    (tmp_path / "release_routines").mkdir()
    (tmp_path / support.POLICY_PATH).write_text(json.dumps(current))
    selected = support.coverage_versions(current)
    assert selected == ["3.12", "3.9"]
    for extra in support.EXTRAS:
        assert any(support.supports_extra(current, extra, version) for version in selected)
    assert any(
        "--ignore=tests/sql/explorer" not in arguments_for_tests(tmp_path, version)
        for version in selected
    )
    assert any(
        "--ignore=tests/datalens_utils" not in arguments_for_tests(tmp_path, version)
        for version in selected
    )
    assert all(support.env_name(version) not in support.local_matrix(current) for version in selected)
    current["minimum"] = "3.10"
    with pytest.raises(ValueError, match="No supported coverage environment for extra tui"):
        support.coverage_versions(current)
