from __future__ import annotations

import re

from tests._support.files import read_json
from tests._support.paths import REPO_ROOT

ROOT = REPO_ROOT


def test_every_github_workflow_is_classified_for_dev() -> None:
    manifest = read_json(ROOT / ".github/required-workflows.json")
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
        if entry["classification"] == "main_only_publish":
            assert "    branches: [main]" in workflow
            assert "github.ref == 'refs/heads/main'" in workflow
        else:
            assert not re.search(r"(?m)^\s{2}push:", workflow)

    for entry in branch["conditional_checks"]:
        assert entry["reason"].strip()
        assert entry["allowed_conclusions"]


def test_dev_workflows_classify_tests_as_required_and_integration_as_advisory() -> None:
    manifest = read_json(ROOT / ".github/required-workflows.json")
    required = {entry["name"]: entry for entry in manifest["branches"]["dev"]["workflows"]}

    assert set(required) == {"tests", "compatibility"}
    assert required["compatibility"]["classification"] == "advisory_push"
    assert required["tests"]["classification"] == "required_push"
    integration = next(
        entry
        for entry in manifest["branches"]["dev"]["classified_non_push_workflows"]
        if entry["name"] == "sql-integration"
    )
    assert integration["events"] == ["workflow_dispatch"]
    assert integration["classification"] == "agent_integration"
    for entry in required.values():
        workflow = (ROOT / entry["path"]).read_text(encoding="utf-8")
        assert re.search(r"(?m)^\s{2}push:", workflow)
        assert entry["allowed_conclusions"] == ["success"]


def test_required_job_names_still_exist_in_workflow_yaml() -> None:
    manifest = read_json(ROOT / ".github/required-workflows.json")
    for workflow_entry in manifest["branches"]["dev"]["workflows"]:
        workflow = (ROOT / workflow_entry["path"]).read_text(encoding="utf-8")
        for job in workflow_entry["required_jobs"]:
            assert f"name: {job['name']}" in workflow
        for job in workflow_entry.get("conditional_jobs", []):
            assert f"name: {job['name']}" in workflow
            assert job["reason"].strip()


def test_wiki_workflow_only_publishes_main_and_uses_separate_credentials() -> None:
    workflow = (ROOT / ".github/workflows/wiki-sync.yml").read_text(encoding="utf-8")
    assert "    branches: [main]" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "          ref: main" in workflow
    assert "persist-credentials: false" in workflow
    assert "secrets.WIKI_SYNC_TOKEN" in workflow
    assert "AGENT_APP_PRIVATE_KEY" not in workflow
    assert "cancel-in-progress: false" in workflow
    assert workflow.index("--check-only") < workflow.index("git -c credential.helper= clone")
    assert "git diff --cached --quiet" in workflow
    assert "git -c credential.helper= push origin HEAD" in workflow
    assert "--force" not in workflow
