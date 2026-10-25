"""Signed task-evidence resolution gates for controller publication."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from tests.repository.github_agent import controller

if TYPE_CHECKING:
    from pathlib import Path


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
    controls.coordinator_held.return_value = False
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
    # Legacy untyped reviews record source-review progress; the controls are mocked here.
    monkeypatch.setattr(controller.trusted_module("source_review"), "begin", Mock())
    # Untyped legacy verdicts continue as pending source reviews (no terminal review).
    assert controller.publish(gh, task, tmp_path, tmp_path) == {"review_status": "pending"}
    assert [call.args[0] for call in gh.api.call_args_list] == ["check-runs"]
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
    with pytest.raises(RuntimeError, match=r"trusted signed head|another producer"):
        controller.publish(gh, task, tmp_path, tmp_path)
    assert not any(call.args[1] == "push" for call in git.call_args_list)
    controller.signing_options.assert_not_called()
    gh.api.assert_not_called()
