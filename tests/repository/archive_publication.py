from __future__ import annotations

import json

import pytest

from tests.repository._support.archive_api import ArchiveAPI, large_context
from tests.repository.review_control import control, controller, publication

__all__ = ["control", "publication"]


def candidate(publication, tmp_path, monkeypatch):
    work, git, commit, task, artifact, gh, pushed, _history = publication
    (work / "feature.txt").write_text("original\n")
    head = commit()
    value = task(head, head, "feature")
    proto = controller.trusted_module("task_context")
    gh.pr["html_url"] = "https://github.com/owner/repo/pull/1"
    previous = value["user_task"]
    context = large_context(proto)
    for field in ["task_id", "producer_id", "starting_prompt"]:
        context[field] = previous[field]
    value.update(
        user_task=context,
        task_digest=proto.digest(context),
        previous_task_digest=proto.digest(previous),
        producer_id=context["producer_id"],
    )
    (work / "feature.txt").write_text("corrected requested feature\n")
    output = artifact(value)
    destination = tmp_path / "github-metadata"
    destination.mkdir()
    api = ArchiveAPI(destination)
    original = gh.api

    def github(path, method="GET", data=None):
        if path.startswith("git/"):
            return api(path, method, data)
        if path == "pulls/" + str(value["number"]) and method == "PATCH":
            gh.pr["body"] = data["body"]
            gh.pr["head"]["sha"] = pushed[-1]
            return gh.pr
        return original(path, method, data)

    monkeypatch.setattr(gh, "api", github)
    return work, git, value, output, gh, pushed, api


def test_oversized_correction_publication_keeps_candidate_tree_and_full_signed_evidence(
    publication,
    tmp_path,
    monkeypatch,
):
    work, git, value, output, gh, pushed, api = candidate(publication, tmp_path, monkeypatch)
    patch = (output / "fix.patch").read_bytes()
    receipt = controller.publish(gh, value, output, work)
    assert receipt["head"] == pushed[-1]
    assert len(pushed) == 1
    assert api.refs
    assert git("diff", "--name-only", value["head"], pushed[-1]).splitlines() == [b"feature.txt"]
    assert git("show", pushed[-1] + ":feature.txt") == b"corrected requested feature\n"
    assert (output / "fix.patch").read_bytes() == patch
    assert controller.task_metadata(gh, gh.pr)["user_task"] == value["user_task"]
    assert len(gh.pr["body"]) < 55000


def test_archive_failure_precedes_feature_push_and_retains_retryable_patch(
    publication,
    tmp_path,
    monkeypatch,
):
    work, git, value, output, gh, pushed, api = candidate(publication, tmp_path, monkeypatch)
    patch = (output / "fix.patch").read_bytes()
    api.fail_after = 1
    with pytest.raises(RuntimeError, match="lost response"):
        controller.publish(gh, value, output, work)
    assert pushed == []
    assert (output / "fix.patch").read_bytes() == patch
    assert not api.refs
    git("reset", "--hard", value["head"])
    api.fail_after = None
    controller.publish(gh, value, output, work)
    assert len(pushed) == 1
    assert controller.task_metadata(gh, gh.pr)["user_task"] == value["user_task"]


def test_new_authoritative_hold_during_archive_creation_blocks_candidate_push(
    publication,
    tmp_path,
    monkeypatch,
    control,
):
    work, _git, value, output, gh, pushed, api = candidate(publication, tmp_path, monkeypatch)
    original = gh.api

    def github(path, method="GET", data=None):
        result = original(path, method, data)
        if path == "git/refs" and method == "POST":
            control.pause(
                gh, gh.pr, "Unresolved provenance must remain held.", "Preserve authority"
            )
        return result

    monkeypatch.setattr(gh, "api", github)
    with pytest.raises(RuntimeError, match="awaiting user"):
        controller.publish(gh, value, output, work)
    assert pushed == []
    assert api.refs  # Only non-executable task data reached its immutable archive.
    assert control.paused(gh, gh.pr)


def test_oversized_final_description_fails_preflight_without_archive_or_feature_writes(
    publication,
    tmp_path,
    monkeypatch,
):
    work, _git, value, output, gh, pushed, api = candidate(publication, tmp_path, monkeypatch)
    old = gh.pr["body"]
    gh.pr["body"] = "Description " * 5000 + old
    with pytest.raises(ValueError, match="description exceeds"):
        controller.publish(gh, value, output, work)
    assert pushed == []
    assert api.calls == []
    assert json.loads((output / "task.json").read_text()) == value
