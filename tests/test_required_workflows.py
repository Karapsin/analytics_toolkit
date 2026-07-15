from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_every_github_workflow_is_classified_for_dev() -> None:
    manifest = json.loads((ROOT / ".github/required-workflows.json").read_text(encoding="utf-8"))
    branch = manifest["branches"]["dev"]
    classified = {entry["path"] for entry in branch["workflows"]}
    classified.update(entry["path"] for entry in branch["classified_non_push_workflows"])
    workflows = {
        path.relative_to(ROOT).as_posix() for path in (ROOT / ".github/workflows").glob("*.yml")
    }

    assert classified == workflows
    for entry in branch["classified_non_push_workflows"]:
        assert entry["reason"].strip()


def test_required_dev_workflows_and_jobs_are_named() -> None:
    manifest = json.loads((ROOT / ".github/required-workflows.json").read_text(encoding="utf-8"))
    required = {entry["name"]: entry for entry in manifest["branches"]["dev"]["workflows"]}

    assert set(required) == {"tests", "sql-integration"}
    assert required["sql-integration"]["required_jobs"] == [
        "core SQL integration",
        "authentication SQL integration",
    ]
