"""Reviewer transport sanitization and signed chronological history boundaries."""

from __future__ import annotations

import json
import uuid

import pytest

from agent_tools import (
    local_review,
    local_review_files,
    review_chunks,
    review_iterations,
    review_waves,
)
from tests.agent_tools.local_review import (
    candidate as candidate,  # noqa: PLC0414 - shared fixture or compatibility export.
)
from tests.agent_tools.local_review_runtimes import (
    adapter_base as adapter_base,  # noqa: PLC0414 - shared fixture or compatibility export.
)
from tests.agent_tools.local_review_runtimes import install_fake
from tests.agent_tools.review_iterations import launch, unreviewed
from tests.agent_tools.review_waves import observed_wave
from tests.agent_tools.sessions import git
from tests.agent_tools.sessions import (
    source as source,  # noqa: PLC0414 - shared fixture or compatibility export.
)

SECRET = "sk-" + "sensitiveReviewerValue" * 2


def test_fake_cli_verdict_strings_are_redacted_before_signing(adapter_base, tmp_path, monkeypatch):
    control, _ = install_fake(tmp_path, monkeypatch, "codex", "approve")
    monkeypatch.setenv("ATK_LOCAL_REVIEWER", "codex")
    script = control / "fake.py"
    script.write_text(
        script.read_text().replace(
            'if mode == "malformed":',
            "verdict.update("
            + repr(
                {
                    "summary": "Reviewer quoted " + SECRET,
                    "change_summary": "Change quoted " + SECRET,
                    "findings": ["[P2] Evidence quoted " + SECRET],
                }
            )
            + ')\nif mode == "malformed":',
        )
    )
    subject = local_review.binding(adapter_base, git(adapter_base, "rev-parse", "HEAD"))
    plan = review_chunks.load(adapter_base, subject, local_review_files)
    request = {
        "subject": subject,
        "discovery_only": False,
        "review_plan": plan,
        "review_chunk": {
            "plan_sha256": review_chunks.plan_digest(plan),
            "wave_id": uuid.uuid4().hex,
            "chunk": plan["chunks"][0],
        },
        "iteration": {
            "attempt_id": uuid.uuid4().hex,
            "recorded_findings": [],
            "prior_findings": [],
            "previous_rounds_sha256": review_iterations.findings_digest([]),
        },
    }
    job = tmp_path / "private-review-job"
    job.mkdir()
    receipt = local_review._execute_review(adapter_base, job, subject, json.dumps(request))
    assert SECRET not in json.dumps(receipt)
    verdict = receipt["payload"]["verdict"]
    assert all("[REDACTED CREDENTIAL]" in verdict[key] for key in ("summary", "change_summary"))
    assert "[REDACTED CREDENTIAL]" in verdict["findings"][0]
    assert SECRET in (job / "verdict.json").read_text()
    local_review.verify_signature(adapter_base, receipt)


@pytest.mark.parametrize("mutation", ["reorder", "delete"])
def test_signed_review_history_cannot_be_reordered_or_deleted(candidate, monkeypatch, mutation):
    unreviewed(candidate)
    launch(
        monkeypatch,
        [
            {"findings": ["[P2] Current issue"], "approved": False},
            {"approved": False, "alignment": "not_aligned"},
        ],
    )
    with pytest.raises(review_iterations.DiscoveryBatchError):
        local_review.review(candidate)
    history = review_iterations.history(candidate)
    current = [
        attempt
        for attempt in history["attempts"]
        if attempt["subject"]["head"] == git(candidate, "rev-parse", "HEAD")
    ]
    assert len(current) == 2
    if mutation == "reorder":
        history["attempts"][-2:] = list(reversed(history["attempts"][-2:]))
    else:
        history["attempts"].pop(-2)
    review_iterations._save(candidate, history)
    with pytest.raises(ValueError, match=r"history|chain|previous"):
        review_iterations.history(candidate)


@pytest.mark.parametrize("mutation", ["reorder", "delete_prefix", "delete_all"])
def test_cached_final_proof_requires_its_validated_signed_history(candidate, monkeypatch, mutation):
    unreviewed(candidate)
    launch(monkeypatch, [{"coverage": False, "approved": False}])
    with pytest.raises(RuntimeError, match="complete diff coverage missing"):
        local_review.review(candidate)
    launch(monkeypatch, [{}])
    receipt = local_review.review(candidate)
    history = review_iterations.history(candidate)
    current = [
        attempt
        for attempt in history["attempts"]
        if attempt["subject"]["head"] == git(candidate, "rev-parse", "HEAD")
    ]
    assert len(current) == 2
    if mutation == "reorder":
        history["attempts"][-2:] = list(reversed(history["attempts"][-2:]))
    elif mutation == "delete_prefix":
        history["attempts"].pop(-2)
    else:
        history["attempts"].clear()
    review_iterations._save(candidate, history)
    with pytest.raises(ValueError, match=r"history|chain|previous"):
        local_review.verified(candidate, receipt["payload"]["head"])


@pytest.mark.parametrize("mutation", ["reorder", "delete_later"])
def test_latest_partial_scope_evidence_cannot_be_hidden_by_wave_journal_order(
    candidate, monkeypatch, mutation
):
    unreviewed(candidate)
    launch(monkeypatch, [{}])
    receipt = local_review.review(candidate)
    subject = local_review.binding(candidate, receipt["payload"]["head"])
    manifest = review_chunks.load(candidate, subject, local_review_files)
    later = observed_wave(
        candidate,
        manifest,
        {
            manifest["chunks"][0]["id"]: {"coverage": False, "approved": False},
        },
    )
    previous = [
        attempt["result"]
        for attempt in review_iterations.history(candidate)["attempts"]
        if "result" in attempt
    ]
    predecessor = review_iterations.findings_digest(previous)
    later["iteration"]["previous_rounds_sha256"] = predecessor
    child = later["outcomes"][0]["result"]
    child["payload"]["previous_rounds_sha256"] = predecessor
    signing = local_review.directory(candidate) / ("later-partial-" + uuid.uuid4().hex)
    signing.mkdir()
    later["outcomes"][0]["result"] = local_review._sign_payload(
        candidate, signing, child["payload"]
    )
    state = review_waves._read(candidate)
    state["waves"].append(later)
    review_waves._save(candidate, state)
    with pytest.raises(ValueError, match="latest valid clearance"):
        local_review.verified(candidate, receipt["payload"]["head"])
    if mutation == "reorder":
        state["waves"][-2:] = list(reversed(state["waves"][-2:]))
    else:
        state["waves"].pop()
    # Simulate storage mutation without asking the coordinator to authorize it.
    review_waves._path(candidate).write_text(json.dumps(state))
    with pytest.raises(ValueError, match=r"history|journal|chain|checkpoint|latest"):
        local_review.verified(candidate, receipt["payload"]["head"])
