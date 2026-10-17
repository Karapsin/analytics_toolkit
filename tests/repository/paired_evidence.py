from __future__ import annotations

import json
from typing import Any

import pytest

from tests._support.paths import REPO_ROOT


@pytest.fixture
def delivery() -> dict[str, Any]:
    path = (
        REPO_ROOT
        / "agent_tools/task_evidence/29adce308110491580e26a80f16fbb4a/parent-objective.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))["delivery_evidence"]


def test_successor_evidence_retains_history_and_exact_request_binding(
    delivery: dict[str, Any],
) -> None:
    # This is an archival transport contract, not a claim about future controller bytes.
    historical = delivery["exact_replay_and_publication_correction"]
    assert historical["successor_paired_validation"]["validated"] is False
    record = delivery["successor_evidence_delivery"]
    companion = record["companion"]
    identity = companion["fixture"]["identity"]
    review = companion["source_review"]
    assert review["candidate_identity"] == identity
    assert identity["dev_base"] == record["dev_base"]
    assert identity["request_id"] == "2eac395a9e9b"
    assert (identity["coding_revision"], identity["observed_revision"]) == (6, 7)
    assert identity["observed_stage"] == "publish"
    binding = companion["checkpoint_review"]["bindings"]
    for key in ("repository", "request_id", "dev_base", "task_digest", "producer_id"):
        assert binding[key] == identity[key]
    assert binding["manifest_sha256"] == identity["complete_checkpoint_source_manifest_sha256"]
    assert binding["patch_sha256"] == identity["checkpoint_patch_sha256"]
    assert review["findings"] == []
    assert review["verdict"] == "approve_successor_companion_source"
    assert review["parent"] == companion["parent"]


def test_successor_sources_reviews_and_ci_travel_together(delivery: dict[str, Any]) -> None:
    record = delivery["successor_evidence_delivery"]
    companion = record["companion"]
    assignment = record["immutable_assignment"]
    sources = list(assignment["source_versions"].values())
    for source in sources:
        digest = assignment["required_files"][source["evidence_file"]]
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")
    review = companion["source_review"]
    for artifact in (companion["fixture"], companion["regression"]):
        versions = [source for source in sources if source["source_path"] == artifact["path"]]
        assert len(versions) == 1
        assert versions[0]["commit"] == companion["commit"]
        assert versions[0]["sha256"] == artifact["sha256"]
        assert review["reviewed_paths"][artifact["path"]]["sha256"] == artifact["sha256"]
    for path, digest in record["controller_hashes"].items():
        assert review["complete_source_identity"][path]["sha256"] == digest
        assert companion["checkpoint_review"]["all_file_hashes"]["work/" + path]["sha256"] == digest
    ci = companion["ci"]
    assert ci["repository"] == companion["repository"]
    assert ci["sha"] == companion["commit"] == "bfb4ba70ff85369292d347ab60a840d9bed133f6"
    assert ci["status"] == "success"
    assert ci["workflows"] == [
        {
            "name": "Repository Checks",
            "status": "completed",
            "conclusion": "success",
            "url": "https://github.com/Karapsin/self_host/actions/runs/39024553657",
        }
    ]


def test_companion_delivery_does_not_grant_controller_or_owner_authority(
    delivery: dict[str, Any],
) -> None:
    record = delivery["successor_evidence_delivery"]
    assert all(value is False for value in record["authority_limits"].values())
    identity = record["companion"]["fixture"]["identity"]
    for key in ("runtime_activated", "protected_approval", "complete_feature_verdict"):
        assert identity[key] is False
    activation = record["companion_activation"]
    assert activation["observed_worker_commit"] == record["companion"]["commit"]
    assert activation["trusted_controller_commit"] != record["starting_head"]
    assert activation["pr33_controller_activated"] is False
    assert record["immutable_assignment"]["author_delivery_verified"] is True
    assert record["immutable_assignment"]["final_published_reviewer_delivery_verified"] is False
