from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from release_routines.lib import check_coverage
from release_routines.lib import python_support as support
from release_routines.lib import python_support_coverage as orchestration
from release_routines.lib.python_support_integration import auth_requirements, integration_plan

from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path


def disjoint_policy(root: Path) -> dict:
    policy = {
        "normal_window": 7,
        "minimum": "3.9",
        "maximum": "3.16",
        "extras": {
            extra: {"minimum": "3.10" if extra == "datalens" else "3.9", "maximum": "3.14"}
            for extra in support.EXTRAS
        },
    }
    policy["extras"]["tui"]["maximum"] = "3.9"
    (root / "release_routines").mkdir()
    (root / support.POLICY_PATH).write_text(json.dumps(policy))
    return policy


@pytest.mark.parametrize("missing_branches", [0, 1])
def test_orchestration_collects_every_run_before_combination_and_ratchets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_branches: int
) -> None:
    policy = disjoint_policy(tmp_path)
    selected = support.coverage_versions(policy)
    commands = []
    stale = orchestration.data_path(tmp_path, selected[0])
    stale.parent.mkdir(parents=True)
    stale.write_text("stale")
    targets = tmp_path / "targets.json"
    targets.write_text(
        json.dumps(
            {
                "overall": {"statements": 100, "branches": 100, "combined": 100},
                "prefixes": {"sql": {"branches": 100}},
            }
        )
    )

    def fake_run(command: list[str], **kwargs: object) -> None:
        assert kwargs == {"cwd": tmp_path, "check": True}
        commands.append(command)
        if command[2] == "tox":
            if len(commands) == 2:
                assert not stale.exists()
            version = selected[len(commands) - 2]
            path = orchestration.data_path(tmp_path, version)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"executed": [len(commands) - 1]}))
        elif command[2:4] == ["coverage", "combine"]:
            assert command[4] == "--keep"
            assert command[5:] == [
                str(orchestration.data_path(tmp_path, version)) for version in selected
            ]
        elif command[2:4] == ["coverage", "json"]:
            (tmp_path / "coverage.json").write_text(
                json.dumps(
                    {
                        "files": {
                            "analytics_toolkit/sql/api.py": {
                                "summary": {
                                    "num_statements": 2,
                                    "missing_lines": 0,
                                    "num_branches": 2,
                                    "missing_branches": missing_branches,
                                },
                            },
                        },
                    }
                )
            )
        elif command[2] == "release_routines.lib.check_coverage":
            assert command[-1] == "--update-targets-managed"
            result = check_coverage.main(
                [str(tmp_path / "coverage.json"), "--targets", str(targets)]
            )
            if result:
                raise subprocess.CalledProcessError(result, command)

    monkeypatch.setattr(orchestration.subprocess, "run", fake_run)
    if missing_branches:
        with pytest.raises(subprocess.CalledProcessError):
            orchestration.run(tmp_path)
    else:
        orchestration.run(tmp_path)
    assert [command[2] for command in commands] == [
        "coverage",
        "tox",
        "tox",
        "coverage",
        "coverage",
        "coverage",
        "release_routines.lib.check_coverage",
    ]


def test_partial_or_incompatible_coverage_plan_cannot_reach_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = disjoint_policy(tmp_path)
    selected = support.coverage_versions(policy)
    monkeypatch.setattr(
        orchestration.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("ran")
    )
    with pytest.raises(ValueError, match="complete policy plan"):
        orchestration.combine(tmp_path, selected[:1])
    with pytest.raises(ValueError, match="Missing required coverage data"):
        orchestration.combine(tmp_path, selected)
    with pytest.raises(ValueError, match="planned compatible interpreter"):
        orchestration.collect(tmp_path, "3.8")


def test_failed_collection_never_combines_stale_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disjoint_policy(tmp_path)
    calls = []

    def fake_run(command: list[str], **_kwargs: object) -> None:
        calls.append(command)
        if command[2] == "tox":
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(orchestration.subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        orchestration.run(tmp_path)
    assert [command[2] for command in calls] == ["coverage", "tox"]


def test_collection_uses_the_interpreters_compatible_suite_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disjoint_policy(tmp_path)
    monkeypatch.setattr(
        orchestration,
        "sys",
        SimpleNamespace(executable=sys.executable, version_info=SimpleNamespace(major=3, minor=9)),
    )
    calls = []
    monkeypatch.setattr(
        orchestration.subprocess, "run", lambda command, **_kwargs: calls.append(command)
    )
    orchestration.collect(tmp_path, "3.9")
    command = calls[0]
    assert command[:4] == [sys.executable, "-m", "coverage", "run"]
    assert "--branch" in command
    assert "--ignore=tests/datalens_utils" in command
    assert "--ignore=tests/sql/explorer" not in command


def test_integration_interpreters_and_constraints_advance_with_the_floor(tmp_path: Path) -> None:
    policy = disjoint_policy(tmp_path)
    shrunk = support.transition(policy, "3.18", {*support.EXTRAS, "all"})
    plan = integration_plan(shrunk)
    assert plan["core"] == "3.12"
    assert {row["airflow"] for row in plan["auth"]["include"]} == {"2", "3"}
    assert all(row["python"] == "3.12" for row in plan["auth"]["include"])
    row = plan["auth"]["include"][0]
    requirements = auth_requirements(REPO_ROOT, row)
    assert "constraints-3.12.txt" in requirements
    assert "constraints-3.11.txt" not in requirements
    assert "apache-airflow==2.10.5" in requirements
    assert "playwright==1.54.0" in requirements
    later = support.transition(shrunk, "3.19", {*support.EXTRAS, "all"})
    assert [row["airflow"] for row in integration_plan(later)["auth"]["include"]] == ["3"]
