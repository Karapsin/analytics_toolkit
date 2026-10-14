"""Keep every active rollout instruction bound to the same reviewed candidate."""

from __future__ import annotations

import re

from tests._support.paths import REPO_ROOT

COMPANION = "8a153830751278c1a0d1ed54ef310d37c5b94932"


def test_active_dependency_instructions_share_final_pin_and_ci_receipt() -> None:
    operations = (REPO_ROOT / "docs/GITHUB_AGENT_OPERATIONS.md").read_text(encoding="utf-8")
    dependency = operations.split("## Updating trusted automation\n", 1)[1].split(
        "### Recovery acceptance", 1
    )[0]
    rollout = operations.split("## Recovered evidence and staged rollout\n", 1)[1]
    assert re.findall(r"\[self_host commit ([0-9a-f]{40})\]", dependency) == [COMPANION]
    assert re.findall(r"dependency candidate is\n`([0-9a-f]{40})`", rollout) == [COMPANION]
    receipt = "operator/companion-8a1538307512-ci.json"
    assert receipt in dependency
    assert receipt in rollout
    assert "/actions/runs/38491515819/job/115617553053" in dependency
    assert "run `38491515819`, job\n`115617553053`" in rollout
    assert "source_files" in dependency
    assert "source_blob_proofs" in dependency
    assert "ancestors are historical evidence" in dependency
