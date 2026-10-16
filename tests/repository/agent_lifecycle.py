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
            "codex/open",
        )
    ]
    merged = [
        {
            "number": number,
            "merged_at": "2026-10-12",
            "head": {"ref": name, "sha": head, "repo": {"full_name": gh.repository}},
        }
        for number, name in enumerate(
            ("codex/merged", "codex/active", "codex/reused", "codex/open"), 1
        )
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
    modules = {"review_control": controls, "branch_cleanup": cleanup}

    def module(name: str) -> Any:
        return modules.get(name) or original_module(name)

    monkeypatch.setattr(controller, "trusted_module", module)
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
    cleanup.cleanup.assert_called_once_with(gh, tmp_path, force=True, active_branches=set())


@pytest.mark.parametrize("gate", ["provenance", "missing-policy", "stale-policy", "veto"])
def test_selected_merge_retains_authorization_gates(
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
    else:
        controls.paused.return_value = True
    controller.merge_selected(gh, 17, tmp_path, pr_numbers={11}, active_branches=set())
    gh.api.assert_not_called()
    controller.dispatch.assert_not_called()
    cleanup.cleanup.assert_not_called()


def test_selected_merge_rejects_changed_task_digest(
    tmp_path: Path, merge_environment: tuple[Any, Any, Any]
) -> None:
    gh, _, cleanup = merge_environment
    controller.task_metadata.side_effect = ValueError("Changed signed task digest")
    with pytest.raises(ValueError, match="signed task digest"):
        controller.merge_selected(gh, 17, tmp_path, pr_numbers={11}, active_branches=set())
    gh.api.assert_not_called()
    controller.dispatch.assert_not_called()
    cleanup.cleanup.assert_not_called()


@pytest.mark.parametrize("gate", ["stale-review", "failed-ci", "missing-ci"])
def test_selected_merge_retains_review_and_ci_gates(
    gate: str, tmp_path: Path, merge_environment: tuple[Any, Any, Any]
) -> None:
    gh, _, cleanup = merge_environment
    if gate == "stale-review":
        gh.checks.return_value[0]["external_id"] = "old-base"
    elif gate == "failed-ci":
        gh.checks.return_value[1]["conclusion"] = "failure"
    else:
        gh.checks.return_value = gh.checks.return_value[:1]
    controller.merge_selected(gh, 17, tmp_path, pr_numbers={11}, active_branches=set())
    gh.api.assert_not_called()
    controller.dispatch.assert_not_called()
    cleanup.cleanup.assert_not_called()


@pytest.mark.parametrize("gate", ["fragment", "base-race"])
def test_selected_merge_retains_publication_gates(
    gate: str, tmp_path: Path, merge_environment: tuple[Any, Any, Any]
) -> None:
    gh, _, cleanup = merge_environment
    if gate == "fragment":
        gh.pages.return_value = [
            {"filename": "agent_tools/changelog/pending.md", "status": "added"}
        ]
    else:
        gh.base.side_effect = ["base", "changed-base"]
    controller.merge_selected(gh, 17, tmp_path, pr_numbers={11}, active_branches=set())
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


def publication_plan(proto: Any, location: str, resolution: str) -> dict[str, Any]:
    text = "Preserve export fidelity."
    approvals = {("approved", "correction"): 1}
    approval_index = 0 if resolution == "unrelated" else approvals.get((resolution, location))
    return {
        "id": proto.plan_id(text),
        "text": text,
        "approval_index": approval_index,
        "starting_prompt_approval": resolution == "approved" and location == "starting",
    }


def publication_blob(context: dict[str, Any], *args: str) -> str | bytes:
    png = b"\x89PNG\r\n\x1a\nsanitized image"
    if args[1] == "ls-tree":
        path = context["images"][0]["path"]
        return ("100644 blob " + "f" * 40 + "\t" + path + "\0").encode()
    return {"-t": "blob", "-s": str(len(png))}.get(args[2], png)


def publication_git(context: dict[str, Any], mode: str, branch: str) -> Any:
    proto = controller.trusted_module("task_context")
    responses = {
        "diff": "tests/regression.py",
        "write-tree": "tree",
        "rev-parse": "tree",
        "commit-tree": "c" * 40,
        "log": "Recovered feature" + proto.trailers(context, context["producer_id"]),
        "ls-remote": "d" * 40 + "\trefs/heads/" + branch if mode == "recovered-feature" else "",
    }

    def command(*args: str, **kwargs: Any) -> str | bytes:
        if args[1] in {"ls-tree", "cat-file"}:
            return publication_blob(context, *args)
        return responses.get(args[1], "")

    return command


def publication_context(reference: str, location: str, resolution: str) -> dict[str, Any]:
    proto = controller.trusted_module("task_context")
    context: dict[str, Any] = {
        "version": 2,
        "task_id": "publication-references",
        "producer_id": "author:test",
        "source": "user",
        "starting_prompt": reference if location == "starting" else "Preserve exports.",
        "corrections": ["Use the written replacement scope."],
        "plans": [],
        "images": [],
        "replacements": [],
        "decisions": [],
        "questions": [],
    }
    if location == "correction":
        context["corrections"].append(reference)
    if resolution in {"approved", "unapproved", "unrelated"}:
        context["plans"].append(publication_plan(proto, location, resolution))
    elif resolution == "image":
        image_hash = hashlib.sha256(b"\x89PNG\r\n\x1a\nsanitized image").hexdigest()
        context["images"].append(
            {
                "reference": reference,
                "description": "Owner-reviewed export layout.",
                "path": f"agent_tools/task_evidence/{context['task_id']}/{image_hash}.png",
                "sha256": image_hash,
                "approval_index": 0,
            }
        )
    elif resolution == "replacement":
        context["replacements"].append(
            {
                "reference": "Implement the plan" if "plan" in reference else reference,
                "user_index": 0,
            }
        )
    return context


@pytest.fixture
def reference_publication(tmp_path: Path, monkeypatch: Any) -> Any:
    proto = controller.trusted_module("task_context")
    controls = Mock()
    controls.paused.return_value = False
    monkeypatch.setattr(controller, "signed_head", Mock(return_value=True))
    original_module = controller.trusted_module
    monkeypatch.setattr(
        controller,
        "trusted_module",
        lambda name: controls if name == "review_control" else original_module(name),
    )
    monkeypatch.setattr(controller, "trusted", Mock(return_value=True))
    monkeypatch.setattr(controller, "authorization_error", Mock(return_value=None))
    monkeypatch.setattr(controller, "verify_correction_plan", Mock())
    monkeypatch.setattr(controller, "signing_options", Mock(return_value=[]))
    monkeypatch.setattr(controller, "monitor", Mock(return_value=(99, {})))

    def prepare(context: dict[str, Any], mode: str) -> tuple[Any, dict[str, Any], Any]:
        kind = "feature" if mode == "recovered-feature" else mode
        number = 0 if kind == "feature" else 1
        task: dict[str, Any] = {
            "number": number,
            "kind": kind,
            "head": "a" * 40,
            "base": "b" * 40,
            "branch": "codex/reference-publication",
            "user_task": context,
            "producer_id": context["producer_id"],
            "task_digest": proto.digest(context),
        }
        (tmp_path / "task.json").write_text(json.dumps(task))
        (tmp_path / "fix.patch").write_text("mocked patch")
        if kind == "repair":
            (tmp_path / "verdict.json").write_text(
                json.dumps({"outcome": "patch", "reason": "repair", "required_evidence": []})
            )
        gh = Mock(repository="owner/repo")
        gh.base.return_value = task["base"]
        gh.pull.return_value = {
            "number": 1,
            "head": {"sha": task["head"]},
            "body": "Feature description.",
            "html_url": "https://github.com/owner/repo/pull/1",
        }
        gh.pulls.return_value = []
        gh.api.return_value = gh.pull.return_value
        monkeypatch.setattr(
            controller, "task_metadata", Mock(return_value={"task_digest": task["task_digest"]})
        )

        git = Mock(side_effect=publication_git(context, mode, task["branch"]))
        monkeypatch.setattr(controller, "command", git)
        return gh, task, git

    return prepare


@pytest.mark.parametrize(
    "mode", ["feature", "recovered-feature", "correction", "conflict", "repair"]
)
@pytest.mark.parametrize("location", ["starting", "correction"])
@pytest.mark.parametrize("failure", ["absent", "unapproved", "unrelated", "image"])
def test_unresolved_references_block_all_publication_writes(
    tmp_path: Path, reference_publication: Any, mode: str, location: str, failure: str
) -> None:
    reference = "[Image #1]" if failure == "image" else "Implement the plan."
    context = publication_context(reference, location, "absent" if failure == "image" else failure)
    proto = controller.trusted_module("task_context")
    # Legacy reading/signature verification stays independent of the publication gate.
    legacy = proto.legacy(context)
    assert proto.parse(proto.render(legacy)) == legacy
    proto.verify_commit(legacy, proto.trailers(legacy))
    gh, task, git = reference_publication(context, mode)
    reason = "Missing image evidence" if failure == "image" else "exact approved plan"
    with pytest.raises(ValueError, match=reason):
        controller.publish(gh, task, tmp_path, tmp_path)
    git.assert_not_called()  # No patch application, signing, recovery fetch or push.
    controller.signing_options.assert_not_called()
    controller.verify_correction_plan.assert_not_called()
    gh.api.assert_not_called()
    gh.comment.assert_called_once()
    assert reason in gh.comment.call_args.args[1]
    if task["number"]:
        gh.label.assert_called_once_with(task["number"], acquire=False)
    else:
        gh.label.assert_not_called()


@pytest.mark.parametrize(
    "mode", ["feature", "recovered-feature", "correction", "conflict", "repair"]
)
@pytest.mark.parametrize("location", ["starting", "correction"])
@pytest.mark.parametrize(
    "resolution", ["approved", "image", "replacement-plan", "replacement-image"]
)
def test_resolved_references_publish_with_real_resolver(
    tmp_path: Path, reference_publication: Any, mode: str, location: str, resolution: str
) -> None:
    reference = "[Image #1]" if "image" in resolution else "Implement the plan."
    context = publication_context(
        reference, location, "replacement" if resolution.startswith("replacement") else resolution
    )
    gh, task, git = reference_publication(context, mode)
    result = controller.publish(gh, task, tmp_path, tmp_path)
    pushes = [call.args for call in git.call_args_list if call.args[1] == "push"]
    assert len(pushes) == 1
    head = "d" * 40 if mode == "recovered-feature" else "c" * 40
    if mode == "recovered-feature":
        controller.signing_options.assert_not_called()
        controller.signed_head.assert_called_once_with(gh.repository, head)
    assert pushes[0][-1] == head + ":refs/heads/" + task["branch"]
    assert gh.api.call_args.args[:2] == (
        ("pulls", "POST") if not task["number"] else ("pulls/1", "PATCH")
    )
    body = gh.api.call_args.args[2]["body"]
    assert controller.trusted_module("task_context").parse(body) == context
    gh.comment.assert_not_called()
    if task["kind"] in {"feature", "correction"}:
        assert result is not None
        assert result["head"] == head
    if task["number"]:
        gh.label.assert_called_once_with(task["number"], acquire=False)


def test_ordinary_v1_context_still_publishes(tmp_path: Path, reference_publication: Any) -> None:
    proto = controller.trusted_module("task_context")
    context = proto.legacy(publication_context("Preserve exports.", "starting", "absent"))
    gh, task, git = reference_publication(context, "feature")
    controller.publish(gh, task, tmp_path, tmp_path)
    assert any(call.args[1] == "push" for call in git.call_args_list)
    assert proto.parse(gh.api.call_args.args[2]["body"]) == context


def test_review_of_unresolved_history_does_not_require_publication_evidence(
    tmp_path: Path, reference_publication: Any, monkeypatch: Any
) -> None:
    proto = controller.trusted_module("task_context")
    context = proto.legacy(publication_context("Implement the plan.", "starting", "absent"))
    gh, task, git = reference_publication(context, "review")
    (tmp_path / "review-execution.json").write_text("{}")
    (tmp_path / "verdict.json").write_text(
        json.dumps(
            {
                "alignment": "aligned",
                "change_summary": "Preserves the feature.",
                "approved": True,
                "summary": "Historical attachment remains verifiable.",
                "findings": [],
            }
        )
    )
    monkeypatch.setattr(controller, "verify_review_execution", Mock())
    controller.publish(gh, task, tmp_path, tmp_path)
    assert [call.args[0] for call in gh.api.call_args_list] == [
        "check-runs",
        "pulls/1/reviews",
    ]
    git.assert_not_called()
    gh.label.assert_not_called()


def test_conflict_marker_check_rejects_before_artifact_serialization(
    tmp_path: Path, monkeypatch: Any
) -> None:
    failure = RuntimeError("docs/CHANGELOG.md:11: leftover conflict marker")
    command = Mock(side_effect=["", failure])
    monkeypatch.setattr(controller, "command", command)
    output = tmp_path / "artifacts"
    with pytest.raises(RuntimeError, match="leftover conflict marker"):
        controller.collect({"kind": "correction", "head": "a" * 40}, tmp_path, output)
    assert command.call_args.args == ("git", "diff", "--cached", "--check")
    assert command.call_count == 2
    assert not (output / "fix.patch").exists()
    assert not (output / "task.json").exists()


def test_corrected_checkpoint_passes_check_and_retains_patch_bytes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    patch = b"retained checkpoint patch\n"
    command = Mock(side_effect=["", "", "", patch])
    monkeypatch.setattr(controller, "command", command)
    output = tmp_path / "artifacts"
    task = {"kind": "correction", "head": "a" * 40}
    controller.collect(task, tmp_path, output)
    assert command.call_args_list[1].args == ("git", "diff", "--cached", "--check")
    assert (output / "fix.patch").read_bytes() == patch
    assert json.loads((output / "task.json").read_text()) == task


@pytest.mark.parametrize("failure", ["signature", "producer"])
def test_recovered_feature_rejects_untrusted_head_before_any_publication(
    tmp_path: Path, reference_publication: Any, monkeypatch: Any, failure: str
) -> None:
    context = publication_context("Preserve exports.", "starting", "absent")
    gh, task, git = reference_publication(context, "recovered-feature")
    if failure == "signature":
        controller.signed_head.return_value = False
    else:
        original = git.side_effect
        proto = controller.trusted_module("task_context")

        def command(*args: str, **kwargs: Any) -> Any:
            if args[1] == "log":
                return "Another producer" + proto.trailers(context, "author:other")
            return original(*args, **kwargs)

        git.side_effect = command
    with pytest.raises(RuntimeError, match="trusted signed head|another producer"):
        controller.publish(gh, task, tmp_path, tmp_path)
    assert not any(call.args[1] == "push" for call in git.call_args_list)
    controller.signing_options.assert_not_called()
    gh.api.assert_not_called()
