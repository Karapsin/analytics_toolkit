from __future__ import annotations

import copy
import hashlib
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from tests.repository.github_agent import checks, controller, owned_pr
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
    assert events == []  # Routine rejection is retained on GitHub, without Telegram noise.
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
            "codex/open",
        )
    ]
    merged = [
        {
            "number": number,
            "merged_at": "2026-10-12",
            "head": {"ref": name, "sha": head, "repo": {"full_name": gh.repository}},
        }
        for number, name in enumerate(("codex/merged", "codex/active", "codex/reused", "codex/open"), 1)
    ]
    branches[4]["commit"]["sha"] = "b" * 40
    gh.pages.side_effect = lambda path: branches if path.startswith("branches") else merged
    gh.pulls.return_value = [{"head": {"ref": "codex/open"}}]
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
        "codex/open",
    }
    assert calls[-1] == (
        "push",
        "--force-with-lease=refs/heads/codex/merged:" + head,
        "origin",
        ":refs/heads/codex/merged",
    )
    assert cleanup.cleanup(gh, tmp_path)["status"] == "not_due"


@pytest.fixture
def merge_environment(monkeypatch: Any) -> tuple[Any, Any, Any]:
    selected = owned_pr()
    selected.update(number=11, mergeable=True, labels=[])
    owned = copy.deepcopy(selected)
    owned["number"] = 7
    owned["labels"] = [{"name": "agent:writing"}]
    gh = Mock(repository="owner/repo")
    gh.pulls.return_value = [owned, selected]
    gh.pull.side_effect = lambda number: {7: owned, 11: selected}[number]
    gh.base.return_value = "base"
    gh.checks.return_value = checks()
    gh.pages.return_value = []
    gh.api.return_value = {"merged": True}
    controls = Mock()
    controls.paused.return_value = False
    cleanup = Mock()
    original_module = controller.trusted_module
    monkeypatch.setattr(
        controller,
        "trusted_module",
        lambda name: {"review_control": controls, "branch_cleanup": cleanup}.get(name)
        or original_module(name),
    )
    monkeypatch.setattr(controller, "authorization_error", Mock(return_value=None))
    monkeypatch.setattr(controller, "task_metadata", Mock(return_value={}))
    monkeypatch.setattr(controller, "monitor", Mock(return_value=(99, {})))
    monkeypatch.setattr(controller, "dispatch", Mock())
    monkeypatch.setattr(controller, "save_monitor", Mock())
    return gh, controls, cleanup


def test_selected_merge_skips_owned_pr_and_preserves_unpublished_branches(
    tmp_path: Path, merge_environment: tuple[Any, Any, Any]
) -> None:
    gh, _, cleanup = merge_environment
    active = {"codex/correction-queued", "codex/telegram-feature-running"}
    controller.merge_selected(gh, 17, tmp_path, pr_numbers={11}, active_branches=active)
    gh.pull.assert_called_once_with(11)
    gh.api.assert_called_once_with(
        "pulls/11/merge", "PUT", {"sha": "a" * 40, "merge_method": "squash"}
    )
    cleanup.cleanup.assert_called_once_with(gh, tmp_path, force=True, active_branches=active)
    controller.dispatch.assert_called_once_with(
        gh, "integration", {"candidate": "a" * 40, "number": 11}
    )


def test_empty_merge_selection_is_inert(
    tmp_path: Path, merge_environment: tuple[Any, Any, Any]
) -> None:
    gh, _, cleanup = merge_environment
    controller.merge_selected(gh, 17, tmp_path, pr_numbers=set(), active_branches=set())
    gh.pull.assert_not_called()
    gh.api.assert_not_called()
    cleanup.cleanup.assert_not_called()


def test_legacy_merge_still_inspects_every_pr(
    tmp_path: Path, merge_environment: tuple[Any, Any, Any]
) -> None:
    gh, _, cleanup = merge_environment
    controller.merge(gh, 17, tmp_path)
    assert [call.args[0] for call in gh.pull.call_args_list] == [7, 11]
    gh.api.assert_called_once()
    cleanup.cleanup.assert_called_once_with(gh, tmp_path, force=True, active_branches=None)


@pytest.mark.parametrize(
    "gate",
    [
        "provenance",
        "missing-policy",
        "stale-policy",
        "veto",
        "task-digest",
        "stale-review",
        "failed-ci",
        "missing-ci",
        "fragment",
        "base-race",
    ],
)
def test_selected_merge_retains_all_gates(
    gate: str,
    tmp_path: Path,
    monkeypatch: Any,
    merge_environment: tuple[Any, Any, Any],
) -> None:
    gh, controls, cleanup = merge_environment
    if gate == "provenance":
        monkeypatch.setattr(controller, "trusted", lambda *args: False)
    elif gate in {"missing-policy", "stale-policy"}:
        controller.authorization_error.return_value = gate
    elif gate == "veto":
        controls.paused.return_value = True
    elif gate == "task-digest":
        controller.task_metadata.side_effect = ValueError("Changed signed task digest")
    elif gate == "stale-review":
        gh.checks.return_value[0]["external_id"] = "old-base"
    elif gate == "failed-ci":
        gh.checks.return_value[1]["conclusion"] = "failure"
    elif gate == "missing-ci":
        gh.checks.return_value = gh.checks.return_value[:1]
    elif gate == "fragment":
        gh.pages.return_value = [
            {"filename": "agent_tools/changelog/pending.md", "status": "added"}
        ]
    elif gate == "base-race":
        gh.base.side_effect = ["base", "changed-base"]
    if gate == "task-digest":
        with pytest.raises(ValueError, match="signed task digest"):
            controller.merge_selected(
                gh, 17, tmp_path, pr_numbers={11}, active_branches=set()
            )
    else:
        controller.merge_selected(
            gh, 17, tmp_path, pr_numbers={11}, active_branches=set()
        )
    gh.api.assert_not_called()
    controller.dispatch.assert_not_called()
    cleanup.cleanup.assert_not_called()


def test_selected_merge_head_race_does_not_clean_branches(
    tmp_path: Path, merge_environment: tuple[Any, Any, Any]
) -> None:
    gh, _, cleanup = merge_environment
    gh.api.return_value = {"merged": False}
    controller.merge_selected(gh, 17, tmp_path, pr_numbers={11}, active_branches=set())
    assert gh.api.call_args.args[2]["sha"] == "a" * 40
    cleanup.cleanup.assert_not_called()
