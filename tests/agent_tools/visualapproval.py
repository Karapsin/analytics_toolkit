"""Source-first captures require the final signed approval for that exact subject."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_tools import local_review
from agent_tools import sql_explorer_visual as visual
from tests.agent_tools._support.local_review import signed_receipt
from tests.agent_tools.local_review_evidence import candidate as candidate  # noqa: PLC0414
from tests.agent_tools.local_review_evidence import commit, visual_candidate
from tests.agent_tools.review_order import ordered_candidate
from tests.agent_tools.sessions import source as source  # noqa: PLC0414


def fake_capture(monkeypatch, after_scene=None):
    calls = []
    real_run = visual._run
    monkeypatch.setattr(visual, "_host_python", lambda _root: "offline-python")
    monkeypatch.setattr(visual, "_validate_png", lambda path: path.read_bytes())

    def run(command, **kwargs):
        if "--scene" not in command:
            return real_run(command, **kwargs)
        scene = command[command.index("--scene") + 1]
        screenshot = Path(command[command.index("--screenshot") + 1])
        geometry = Path(command[command.index("--evidence") + 1])
        screenshot.write_bytes(b"offline PNG boundary")
        geometry.write_text(json.dumps({"scene_id": scene, "ok": True}))
        calls.append(scene)
        if after_scene is not None and len(calls) == 1:
            after_scene()
        return None

    monkeypatch.setattr(visual, "_run", run)
    return calls


def complete_capture(root, review_id, monkeypatch):
    calls = fake_capture(monkeypatch)
    visual.start_review(root, review_id)
    visual.capture_review(root, review_id)
    session = visual._load_session(root, review_id)
    for scene in session["scenes"]:
        visual.record_review(root, review_id, scene, "pass")
    assert visual.complete_review(root, review_id)["ok"] is True
    assert calls
    return json.loads((root / visual.RECEIPT).read_text())


def test_source_first_start_rejects_missing_or_intermediate_approval(candidate, monkeypatch):
    ordered_candidate(candidate)
    calls = fake_capture(monkeypatch)
    before = list((candidate / visual.SESSIONS).glob("*.json"))
    result = visual.visual_workflow("start", "review-early", str(candidate))
    assert result["ok"] is False
    assert "Final source approval" in result["error"]
    assert calls == []
    assert list((candidate / visual.SESSIONS).glob("*.json")) == before


def test_signed_final_approval_precedes_every_source_first_capture(candidate, monkeypatch):
    ordered_candidate(candidate)
    receipt = signed_receipt(candidate)
    completed = complete_capture(candidate, "review-after", monkeypatch)
    approval = completed["source_approval"]
    assert approval["head"] == receipt["payload"]["head"]
    assert approval["task_digest"] == receipt["payload"]["task_digest"]
    assert approval["receipt_sha256"] == local_review.protocol(candidate).digest(receipt)
    assert visual.verify_visual_receipt(candidate, for_push=True)["ok"] is True


@pytest.mark.parametrize("record", ["receipt", "session", "both"])
def test_older_same_fingerprint_capture_without_final_approval_proof_is_rejected(
    candidate, monkeypatch, record
):
    ordered_candidate(candidate)
    signed_receipt(candidate)
    completed = complete_capture(candidate, "review-proof", monkeypatch)
    fingerprint = visual.content_fingerprint(candidate)
    if record in {"receipt", "both"}:
        completed.pop("source_approval")
        (candidate / visual.RECEIPT).write_text(json.dumps(completed))
    if record in {"session", "both"}:
        path = visual.session_path(candidate, completed["review_id"])
        session = json.loads(path.read_text())
        session.pop("source_approval")
        path.write_text(json.dumps(session))
    assert visual.content_fingerprint(candidate) == fingerprint
    result = visual.verify_visual_receipt(candidate, for_push=True)
    assert result["ok"] is False
    assert "after the current final source approval" in result["message"]


@pytest.mark.parametrize("action", ["capture", "complete"])
def test_changed_source_after_start_invalidates_capture_and_completion(
    candidate, monkeypatch, action
):
    ordered_candidate(candidate)
    signed_receipt(candidate)
    fake_capture(monkeypatch)
    visual.start_review(candidate, "review-stale")
    if action == "complete":
        visual.capture_review(candidate, "review-stale")
        for scene in visual._load_session(candidate, "review-stale")["scenes"]:
            visual.record_review(candidate, "review-stale", scene, "pass")
    commit(candidate, {"analytics_toolkit/sql_explorer/app.py": "APP = 2\n"})
    result = visual.visual_workflow(action, "review-stale", str(candidate))
    assert result["ok"] is False
    assert "Final source approval" in result["error"]
    assert not (candidate / visual.RECEIPT).exists()


def test_source_change_during_capture_prevents_successful_capture_state(candidate, monkeypatch):
    ordered_candidate(candidate)
    signed_receipt(candidate)
    visual.start_review(candidate, "review-raced")
    fake_capture(monkeypatch, lambda: commit(candidate, {"changed-during-capture": "unreviewed\n"}))
    with pytest.raises(visual.VisualReviewError, match="Final source approval"):
        visual.capture_review(candidate, "review-raced")
    session = visual._load_session(candidate, "review-raced")
    assert session["capture"]["status"] == "failed"
    assert session["capture"]["workspace_removed"] is True
    assert not (candidate / visual.RECEIPT).exists()


def test_default_visual_order_keeps_existing_source_independent_start(candidate):
    visual_candidate(candidate)
    session = visual.start_review(candidate, "review-default")
    assert "source_approval" not in session


def test_source_first_capture_cannot_reuse_a_different_final_approval(candidate, monkeypatch):
    ordered_candidate(candidate)
    signed_receipt(candidate)
    completed = complete_capture(candidate, "review-reapproved", monkeypatch)
    changed = signed_receipt(candidate)
    assert (
        local_review.protocol(candidate).digest(changed)
        != completed["source_approval"]["receipt_sha256"]
    )
    result = visual.verify_visual_receipt(candidate, for_push=True)
    assert result["ok"] is False
    assert "after the current final source approval" in result["message"]
