from __future__ import annotations

import copy
import hashlib
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from tests.repository.github_agent import controller
from tests.repository.review_control import GitHub

if TYPE_CHECKING:
    from pathlib import Path


def test_rejection_queues_fresh_correction_without_user_resume(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    control = controller.trusted_module("review_control")
    gh = GitHub()
    task = {"task_digest": "d" * 64}
    control.reject(gh, gh.pr, task, "Missing requested receipt validation.", "Adds PR receipts")
    assert not control.paused(gh, gh.pr)
    assert control.correction(gh.repository, 1, gh.pr["head"]["sha"], task["task_digest"])
    assert not control.correction(gh.repository, 1, "b" * 40, task["task_digest"])
    assert not control.correction(gh.repository, 1, gh.pr["head"]["sha"], "e" * 64)
    control.reject(gh, gh.pr, task, "Missing requested receipt validation.", "Adds PR receipts")
    events = control.pending()
    assert len(events) == 1
    assert events[0]["token"] is None
    assert "no resume command" in json.loads(events[0]["payload"])["command"]
    assert gh.comments[0]["body"].count("What changed:") == 1


def test_question_answer_cannot_clear_owner_veto(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    control = controller.trusted_module("review_control")
    gh = GitHub()
    control.hold_correction(gh, gh.pr)
    assert control.paused(gh, gh.pr)
    control.resume_correction(gh, gh.pr)
    assert not control.paused(gh, gh.pr)
    control.pause(gh, gh.pr, "Owner disapproved protected changes.", "Changed controller")
    control.resume_correction(gh, gh.pr)
    assert control.paused(gh, gh.pr)
    with pytest.raises(ValueError, match="owner/security"):
        control.hold_correction(gh, gh.pr)


def test_approval_notice_requires_current_diff_assessment(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    control = controller.trusted_module("review_control")
    proto = controller.trusted_module("task_context")
    task = proto.repair_context("task", "author:test")
    gh = GitHub()
    paths = [".github/agent/controller.py"]
    control.approval_notice(gh, gh.pr, paths, task)
    assert not control.pending()
    control.assess(
        gh.repository,
        1,
        gh.pr["head"]["sha"],
        proto.digest(task),
        "Validates independent plan receipts.",
    )
    control.approval_notice(gh, gh.pr, paths, task)
    assert (
        json.loads(control.pending()[0]["payload"])["changes"]
        == "Validates independent plan receipts."
    )
    revised = copy.deepcopy(task)
    revised["corrections"].append("Require branch cleanup too.")
    control.approval_notice(gh, gh.pr, paths, revised)
    gh.pr["head"]["sha"] = "b" * 40
    control.approval_notice(gh, gh.pr, paths, task)
    assert len(control.pending()) == 1


def test_repair_discovery_initializes_prepare_context(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        controller.trusted_module("repair_evidence"), "inventory", lambda gh, runs: []
    )
    state = {
        "runs": {"1": {"sha": "old", "status": "completed", "needs": ["HTTP"], "merged": True}}
    }
    task = controller._integration_tasks(object(), state, "a" * 40, [])[0]
    control = tmp_path / "control"
    prompts = control / ".github/agent/prompts"
    prompts.mkdir(parents=True)
    (prompts / "repair.md").write_text("Repair failures without local tests.\n")
    monkeypatch.setattr(controller, "command", lambda *args, **kwargs: "CI failure evidence")
    controller.prepare(Mock(repository="owner/repo"), task, tmp_path, control)
    metadata = json.loads((control / ".github/agent/task.json").read_text())
    assert metadata["producer_id"] == "standing-repair:" + "a" * 40
    assert metadata["task_digest"] == controller.trusted_module("task_context").digest(
        metadata["user_task"]
    )


def test_correction_requires_independent_exact_plan_receipt(tmp_path: Path) -> None:
    task = {
        "head": "a" * 40,
        "base": "b" * 40,
        "task_digest": "c" * 64,
        "plan_hash": "d" * 64,
        "producer_id": "author:one",
    }
    plan = "Fix the exact reviewer findings and preserve feature behavior."
    (tmp_path / "approved-plan.md").write_text(plan)
    task["plan_hash"] = hashlib.sha256(
        (task["task_digest"] + task["base"] + plan + task["head"]).encode()
    ).hexdigest()
    receipt = {**task, "reviewer_id": "plan-reviewer:two", "mode": "read-only", "approved": True}
    path = tmp_path / "plan-review.json"
    path.write_text(json.dumps(receipt))
    controller.verify_correction_plan(task, tmp_path)
    for change in (
        {"head": "e" * 40},
        {"plan_hash": "f" * 64},
        {"mode": "workspace-write"},
        {"reviewer_id": "author:one"},
        {"approved": False},
    ):
        path.write_text(json.dumps({**receipt, **change}))
        with pytest.raises(RuntimeError, match="independent approved plan"):
            controller.verify_correction_plan(task, tmp_path)


def test_cleanup_uses_merged_head_receipt_for_squash_and_leased_delete(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    cleanup = controller.trusted_module("branch_cleanup")
    gh = Mock(repository="owner/repo")
    head = "a" * 40
    branches = [
        {"name": name, "protected": False, "commit": {"sha": head}}
        for name in (
            "dev",
            "main",
            "codex/merged",
            "codex/active",
            "codex/reused",
            "codex/unsubmitted",
        )
    ]
    merged = [
        {
            "number": number,
            "merged_at": "2026-10-12",
            "head": {"ref": name, "sha": head, "repo": {"full_name": gh.repository}},
        }
        for number, name in enumerate(("codex/merged", "codex/active", "codex/reused"), 1)
    ]
    branches[4]["commit"]["sha"] = "b" * 40
    gh.pages.side_effect = lambda path: branches if path.startswith("branches") else merged
    gh.pulls.return_value = []
    gh.pull.side_effect = lambda number: merged[number - 1]
    calls: list[tuple[str, ...]] = []

    def git(work: Path, *args: str) -> str:
        calls.append(args)
        return head + "\trefs/heads/codex/merged" if args[0] == "ls-remote" else ""

    monkeypatch.setattr(cleanup, "_git", git)
    monkeypatch.setattr(cleanup, "_eligible", lambda *args: True)
    result = cleanup.cleanup(gh, tmp_path, active_branches={"codex/active"})
    assert result["deleted"] == ["codex/merged"]
    assert set(result["preserved"]) == {
        "dev",
        "main",
        "codex/active",
        "codex/reused",
        "codex/unsubmitted",
    }
    assert calls[-1] == (
        "push",
        "--force-with-lease=refs/heads/codex/merged:" + head,
        "origin",
        ":refs/heads/codex/merged",
    )
    assert cleanup.cleanup(gh, tmp_path)["status"] == "not_due"
