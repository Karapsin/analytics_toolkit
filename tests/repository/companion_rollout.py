"""Keep historical, active and proposed rollout evidence distinct."""

from __future__ import annotations

import re

from tests._support.paths import REPO_ROOT

COMPANION = "ab7f83cddb6981ee380d3ae76fbc51ba1a41c87a"
ACTIVE_RECEIPT = "operator/worker-reference-replacements-ab7f83cddb69-active.json"
HISTORICAL_RECEIPT = "operator/companion-0c1f1abea3f7-ci-review-and-source.json"


def test_active_dependency_instructions_share_final_pin_and_ci_receipt() -> None:
    operations = (REPO_ROOT / "docs/GITHUB_AGENT_OPERATIONS.md").read_text(encoding="utf-8")
    dependency = operations.split("## Updating trusted automation\n", 1)[1].split(
        "### Recovery acceptance", 1
    )[0]
    rollout = operations.split("## Recovered evidence and staged rollout\n", 1)[1]
    assert re.findall(r"\[self_host commit ([0-9a-f]{40})\]", dependency) == [COMPANION]
    assert re.findall(r"dependency candidate is\n`([0-9a-f]{40})`", rollout) == [COMPANION]
    for section in (dependency, rollout):
        assert ACTIVE_RECEIPT in section
        assert HISTORICAL_RECEIPT in section
        assert "38931069859" in section
    assert "source_files" in dependency
    assert "ancestors are historical evidence" in dependency
    assert "active runtime" in rollout
    assert "PR #15 remains subject to its own fresh" in rollout
    assert "protected approval" in rollout
