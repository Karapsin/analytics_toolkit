"""Explicit task-bound source-first review still requires final visual checks."""

from __future__ import annotations

import json

import pytest

from agent_tools import local_review, mcp_server, sessions, sql_explorer_visual, task_context
from tests.agent_tools._support.local_review import signed_receipt
from tests.agent_tools.local_review_evidence import (
    candidate as candidate,  # noqa: PLC0414 - shared real signed feature fixture.
)
from tests.agent_tools.local_review_evidence import (
    capture_review,
    commit,
    complete_visual_review,
    visual_candidate,
)
from tests.agent_tools.sessions import git
from tests.agent_tools.sessions import source as source  # noqa: PLC0414

DIRECTIVE = "do visual checks only after final review approval"


def ordered_candidate(root, prompt=DIRECTIVE):
    task_context.record(root, prompt, append=True)
    visual_candidate(root)
    context = task_context.read(root)
    digest = local_review.binding(root, git(root, "rev-parse", "HEAD"))["task_digest"]
    decision = {
        "decision": "source-review-before-visuals",
        "message_source": "user",
        "correction_index": len(context["corrections"]) - 1,
        "user_prompt": prompt,
    }
    path = sessions.state_path(root).with_name("atk-local-review-evidence.json")
    path.write_text(json.dumps({"task_digest": digest, "visual_review_order": decision}))
    return path


@pytest.mark.parametrize(
    "prompt", [DIRECTIVE, "  Do VISUAL checks only after final review approval! "]
)
def test_recorded_explicit_order_allows_source_review_without_claiming_visual_pass(
    candidate, monkeypatch, prompt
):
    ordered_candidate(candidate, prompt)
    calls = capture_review(monkeypatch)
    local_review.review(candidate)
    assert len(calls) == 2
    call = calls[0]
    evidence = call["request"]["evidence"]
    assert local_review.VISUAL_EVIDENCE_KEY not in evidence
    assert evidence["visual_review_order"]["user_prompt"] == prompt
    assert evidence["visual_review_order"]["visual_review_required_before_publication"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_prompt", DIRECTIVE + "!"),
        ("correction_index", 999),
        ("correction_index", True),
        ("message_source", "assistant"),
        ("decision", "skip-visuals"),
        ("unexpected", "waiver"),
    ],
)
def test_spoofed_or_invalid_order_cannot_start_source_review(candidate, monkeypatch, field, value):
    path = ordered_candidate(candidate)
    data = json.loads(path.read_text())
    data["visual_review_order"][field] = value
    path.write_text(json.dumps(data))
    calls = capture_review(monkeypatch)
    with pytest.raises(ValueError, match="exact recorded user correction"):
        local_review.review(candidate)
    assert calls == []


def test_unrelated_recorded_user_message_is_not_visual_order_authorization(candidate, monkeypatch):
    ordered_candidate(candidate, "WTF")
    calls = capture_review(monkeypatch)
    with pytest.raises(ValueError, match="exact recorded user correction"):
        local_review.review(candidate)
    assert calls == []


def test_absent_order_keeps_default_completed_visual_requirement(candidate, monkeypatch):
    path = ordered_candidate(candidate)
    path.unlink()
    calls = capture_review(monkeypatch)
    with pytest.raises(RuntimeError, match="Completed SQL Explorer visual review"):
        local_review.review(candidate)
    assert calls == []


def test_order_is_bound_to_current_task_and_source_receipt(candidate):
    ordered_candidate(candidate)
    receipt = signed_receipt(candidate)
    task_context.record(candidate, "Additional requirement", append=True)
    with pytest.raises(ValueError, match="different task"):
        local_review.visual_review_order(candidate)
    with pytest.raises(ValueError, match=r"task|digest"):
        local_review.verified(candidate, receipt["payload"]["head"])


def test_later_visual_completion_preserves_source_approval_but_source_edits_do_not(candidate):
    ordered_candidate(candidate)
    receipt = signed_receipt(candidate)
    head = receipt["payload"]["head"]
    assert not sql_explorer_visual.verify_visual_receipt(candidate, for_push=True)["ok"]
    complete_visual_review(candidate)
    assert sql_explorer_visual.verify_visual_receipt(candidate, for_push=True)["ok"]
    assert local_review.verified(candidate, head) == receipt
    changed = commit(candidate, {"analytics_toolkit/sql_explorer/app.py": "APP = 2\n"})
    with pytest.raises(RuntimeError, match="Fresh independent local review"):
        local_review.verified(candidate, changed)
    assert not sql_explorer_visual.verify_visual_receipt(candidate, for_push=True)["ok"]


def test_source_approval_precedes_visual_gate_and_publication_cannot_skip_it(
    candidate, monkeypatch
):
    ordered_candidate(candidate)
    order = []

    def review(root):
        order.append("review")
        head = git(root, "rev-parse", "HEAD")
        if (local_review.directory(root) / (head + ".json")).exists():
            return local_review.verified(root, head)
        return signed_receipt(root)

    def publish(root):
        order.append("publish")
        return {"pr": 123}

    monkeypatch.setattr(local_review, "review", review)
    monkeypatch.setattr(sessions, "push", publish)
    blocked = mcp_server._push_dev_result(candidate)
    assert order == ["review"]
    assert blocked["blockers"][0]["phase"] == "visual_review"
    assert blocked["readiness"]["source_review"]["status"] == "approved"
    assert blocked["readiness"]["source_review"]["head"] == git(candidate, "rev-parse", "HEAD")
    complete_visual_review(candidate)
    passed = mcp_server._push_dev_result(candidate)
    assert order == ["review", "review", "publish"]
    assert passed["blockers"] == []


def test_commit_can_create_review_candidate_but_cannot_publish_without_visuals(
    candidate, monkeypatch
):
    ordered_candidate(candidate)
    (candidate / "analytics_toolkit/sql_explorer/app.py").write_text("APP = 3\n")
    monkeypatch.setattr(mcp_server, "_verify_precommit_success", lambda root: {"ok": True})
    monkeypatch.setattr(mcp_server, "_version_bump_requirement", lambda *args: {"missing": []})
    calls = capture_review(monkeypatch)
    result = mcp_server.git_workflow(
        action="commit",
        message="Review before visual checks",
        paths=["analytics_toolkit/sql_explorer/app.py"],
        root=str(candidate),
    )
    assert len(calls) == 2
    assert result["ok"] is False
    assert result["blockers"][0]["phase"] == "visual_review"
    assert git(candidate, "log", "-1", "--format=%s") == "Review before visual checks"


def test_default_publication_checks_visuals_before_starting_source_review(candidate, monkeypatch):
    visual_candidate(candidate)
    calls = capture_review(monkeypatch)
    blocked = mcp_server._push_dev_result(candidate)
    assert blocked["blockers"][0]["phase"] == "visual_review"
    assert calls == []
