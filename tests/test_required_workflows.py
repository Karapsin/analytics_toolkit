from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_every_github_workflow_is_classified_for_dev() -> None:
    manifest = json.loads((ROOT / ".github/required-workflows.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    branch = manifest["branches"]["dev"]
    classified = {entry["path"] for entry in branch["workflows"]}
    classified.update(entry["path"] for entry in branch["classified_non_push_workflows"])
    workflows = {
        path.relative_to(ROOT).as_posix() for path in (ROOT / ".github/workflows").glob("*.yml")
    }

    assert classified == workflows
    for entry in branch["classified_non_push_workflows"]:
        assert entry["reason"].strip()
        workflow = (ROOT / entry["path"]).read_text(encoding="utf-8")
        assert not re.search(r"(?m)^\s{2}push:", workflow)

    for entry in branch["conditional_checks"]:
        assert entry["reason"].strip()
        assert entry["allowed_conclusions"]


def test_required_dev_workflows_and_jobs_are_named() -> None:
    manifest = json.loads((ROOT / ".github/required-workflows.json").read_text(encoding="utf-8"))
    required = {entry["name"]: entry for entry in manifest["branches"]["dev"]["workflows"]}

    assert set(required) == {"tests", "sql-integration"}
    assert [job["name"] for job in required["sql-integration"]["required_jobs"]] == [
        "core SQL integration (HTTP + native)",
        "authentication SQL integration (HTTP + native)",
    ]
    assert all(entry["classification"] == "required_push" for entry in required.values())
    for entry in required.values():
        workflow = (ROOT / entry["path"]).read_text(encoding="utf-8")
        assert re.search(r"(?m)^\s{2}push:", workflow)
        assert entry["allowed_conclusions"] == ["success"]


def test_required_job_names_still_exist_in_workflow_yaml() -> None:
    manifest = json.loads((ROOT / ".github/required-workflows.json").read_text(encoding="utf-8"))
    for workflow_entry in manifest["branches"]["dev"]["workflows"]:
        workflow = (ROOT / workflow_entry["path"]).read_text(encoding="utf-8")
        for job in workflow_entry["required_jobs"]:
            assert f"name: {job['name']}" in workflow
        for job in workflow_entry.get("conditional_jobs", []):
            assert f"name: {job['name']}" in workflow
            assert job["reason"].strip()
