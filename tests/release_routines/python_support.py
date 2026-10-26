from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from release_routines.lib import python_support

from tests._support.paths import REPO_ROOT


def _policy(**changes: Any) -> dict[str, Any]:
    policy = json.loads((REPO_ROOT / python_support.POLICY_PATH).read_text(encoding="utf-8"))
    policy.update(changes)
    return dict(policy)


def _copy_repository_metadata(tmp_path: Path) -> Path:
    for relative in (
        "pyproject.toml",
        "README.md",
        "tox.ini",
        str(python_support.POLICY_PATH),
        str(python_support.DOC_PATH),
        "constraints/py39-min.txt",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, target)
    return tmp_path


def test_repository_metadata_matches_committed_python_support_policy() -> None:
    policy = python_support.load_policy(REPO_ROOT)
    assert python_support.consistency_failures(REPO_ROOT, policy) == []
    assert python_support.supported_minors(policy)[-1] == policy["newest"]
    assert len(python_support.supported_minors(policy)) == python_support.window_size(
        policy["lagging_extras"]
    )


def test_fast_tests_workflow_keeps_python_314_only_for_everyday_checks() -> None:
    workflow = (REPO_ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    tests_job = workflow.split("\n  tests:\n", 1)[1].split("\n  quality:\n", 1)[0]
    assert re.findall(r'python-version: "([0-9.]+)"', tests_job) == ["3.14"]
    assert "matrix" not in tests_job
    assert "python_support" not in workflow


def test_compatibility_workflow_reads_matrix_and_runs_drift_check_after_merge() -> None:
    workflow = (REPO_ROOT / ".github/workflows/compatibility.yml").read_text(encoding="utf-8")
    assert "python -m release_routines.lib.python_support check" in workflow
    assert "python -m release_routines.lib.python_support matrix" in workflow
    assert "python_support_drift released" in workflow
    assert "python_support_drift probe" in workflow
    assert "python_support_drift evaluate" in workflow
    assert "needs: [policy, tests, sdk-conformance, python-support-drift]" in workflow
    triggers = workflow.split("\npermissions:", 1)[0]
    assert "branches: [dev, main]" in triggers
    assert "pull_request:\n    branches: [main]" in triggers


@pytest.mark.parametrize(
    ("newest", "lagging", "minimum"),
    [("3.14", [], "3.8"), ("3.15", [], "3.9"), ("3.15", ["airflow"], "3.8")],
)
def test_window_is_seven_minors_and_extends_to_eight_for_lagging_extras(
    newest: str, lagging: list[str], minimum: str
) -> None:
    assert python_support.expected_minimum(newest, lagging) == minimum
    policy = _policy(newest=newest, lagging_extras=lagging, minimum=minimum)
    python_support.validate_policy(policy)
    assert python_support.requires_python(policy) == f">={minimum},<3.{int(newest[2:]) + 1}"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"minimum": "3.9"}, "expected 3.8"),
        ({"lagging_extras": ["airflow"]}, "expected 3.7"),
        ({"lagging_extras": ["tui", "airflow"], "minimum": "3.7"}, "sorted"),
        ({"lagging_extras": ["airflow", "airflow"]}, "duplicates"),
        ({"lagging_extras": ["mystery"]}, "Unknown lagging"),
        ({"normal_window": 5}, "seven minors"),
        ({"extras": ["airflow"]}, "Policy extras"),
        ({"runtime_floor": "3.15"}, "runtime_floor"),
        ({"extra_floors": {"datalens": "3.7"}}, "Invalid Python floor"),
        ({"extra_floors": {"other": "3.10"}}, "Invalid Python floor"),
        ({"newest": "4.0"}, "Python 3 minor"),
        ({"schema_version": 2}, "exactly"),
    ],
)
def test_invalid_policies_are_rejected(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(python_support.PolicyError, match=message):
        python_support.validate_policy(_policy(**changes))


def test_matrix_skips_lagging_extras_only_on_newest_minor() -> None:
    policy = _policy(newest="3.15", minimum="3.8", lagging_extras=["datalens", "tui"])
    rows = {row["env_name"]: row for row in python_support.compatibility_matrix(policy)["include"]}
    assert rows["py315-latest"]["install_target"] == "."
    assert rows["py314-latest"]["install_target"] == ".[tui,datalens]"
    assert rows["py39-min"]["constraints"] == "constraints/py39-min.txt"
    assert "py38-latest" not in rows
    partial = _policy(newest="3.15", minimum="3.8", lagging_extras=["tui"])
    assert python_support.install_target(partial, "3.15") == ".[datalens]"
    table = python_support.render_support_table(partial)
    assert "| `tui` extra | 3.8-3.14 |" in table
    assert "| `datalens` extra | 3.10-3.15 |" in table
    assert "Current window: 8 minors ending at 3.15" in table


def test_consistency_reports_each_drifted_file(tmp_path: Path) -> None:
    root = _copy_repository_metadata(tmp_path)
    policy = _policy(newest="3.15", minimum="3.9")
    (root / "constraints/py39-min.txt").unlink()
    failures = "\n".join(python_support.consistency_failures(root, policy))
    assert "requires-python must be '>=3.9,<3.16'" in failures
    assert "classifiers must list exactly 3.9" in failures
    assert "README.md Depends" in failures
    assert "support table is stale" in failures
    assert "tox.ini env_list is missing py315-latest" in failures
    assert "Missing minimum dependency constraints" in failures


def test_sync_regenerates_metadata_and_leaves_reviewed_profiles(tmp_path: Path) -> None:
    root = _copy_repository_metadata(tmp_path)
    policy = _policy(newest="3.15", minimum="3.8", lagging_extras=["airflow"])
    changed = python_support.sync(root, policy)
    assert set(changed) == {Path("pyproject.toml"), Path("README.md"), python_support.DOC_PATH}
    failures = python_support.consistency_failures(root, policy)
    assert failures == ["tox.ini env_list is missing py315-latest"]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.8,<3.16"' in pyproject
    assert '"Programming Language :: Python :: 3.15",\n  "Topic :: Database",' in pyproject
    assert python_support.sync(root, policy) == []


def test_extra_declarations_must_match_policy(tmp_path: Path) -> None:
    root = _copy_repository_metadata(tmp_path)
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    (root / "pyproject.toml").write_text(
        pyproject.replace(
            "[project.optional-dependencies]\n", "[project.optional-dependencies]\nnew = []\n"
        ),
        encoding="utf-8",
    )
    (root / python_support.DOC_PATH).unlink()
    failures = "\n".join(python_support.consistency_failures(root, _policy()))
    assert "extras" in failures
    assert "differ from the policy" in failures
    assert "support table is stale" in failures


def test_cli_prints_matrix_and_reports_invalid_or_drifted_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert python_support.main(["matrix", "--root", str(REPO_ROOT)]) == 0
    assert json.loads(capsys.readouterr().out)["include"][0]["env_name"] == "py39-latest"
    assert python_support.main(["check", "--root", str(REPO_ROOT)]) == 0
    root = _copy_repository_metadata(tmp_path)
    policy_path = root / python_support.POLICY_PATH
    policy_path.write_text(json.dumps(_policy(minimum="3.9")), encoding="utf-8")
    assert python_support.main(["check", "--root", str(root)]) == 1
    assert "Invalid Python support policy" in capsys.readouterr().err
    policy_path.write_text(json.dumps(_policy(newest="3.15", minimum="3.9")), encoding="utf-8")
    assert python_support.main(["check", "--root", str(root)]) == 1
    assert "Python support metadata drift" in capsys.readouterr().err
    assert python_support.main(["sync", "--root", str(root)]) == 1
    output = capsys.readouterr()
    assert "updated pyproject.toml" in output.out
    assert "py315-latest" in output.err
