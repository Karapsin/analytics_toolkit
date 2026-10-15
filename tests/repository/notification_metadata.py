from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from tests.repository.github_agent import controller
from tests.repository.review_control import GitHub
from tests.repository.review_control import control as control  # noqa: PLC0414 - pytest fixture


@pytest.mark.parametrize("branch", ["codex/orchestrator-task", "codex/telegram-feature-task"])
@pytest.mark.parametrize(
    ("reason", "category"),
    [
        ("Feature context does not match the host-owned request.", "coordinator_blocked"),
        ("Supply the original image.", "paused"),
    ],
)
def test_pause_serializes_lookup_hints_without_asserting_delivery_permission(
    control: Any,
    branch: str,
    reason: str,
    category: str,
) -> None:
    gh = GitHub()
    gh.pr["head"]["ref"] = branch
    # Neither PR metadata nor branch naming can supply authenticated origin.
    gh.pr["request_id"] = "forged-request"
    gh.pr["turn_revision"] = 99
    gh.pr["notification_category"] = "owner_approval"
    gh.pr["request_origin"] = {"verified": True}
    original = copy.deepcopy(gh.pr["head"])
    control.pause(gh, gh.pr, reason, "Concrete controller change")
    control.pause(gh, gh.pr, reason, "Duplicate notice")
    events = control.pending()
    assert len(events) == 1
    event = events[0]
    payload = json.loads(event["payload"])
    assert event["kind"] == "rejection"
    assert event["token"] is None
    assert payload["notification_category"] == category
    assert payload["request_origin"] == {"branch": branch, "task_digest": None}
    assert payload["repository"] == gh.repository
    assert payload["number"] == gh.pr["number"]
    assert payload["head"] == original["sha"]
    assert "request_id" not in payload
    assert "turn_revision" not in payload
    assert "eligible" not in payload
    assert gh.pr["head"] == original
    assert control.paused(gh, gh.pr)
    assert not control.approved(gh.repository, gh.pr["number"], original["sha"])


@pytest.mark.parametrize("branch", ["codex/orchestrator-task", "codex/telegram-feature-task"])
def test_routine_rejection_persists_task_binding_and_deduplicates(
    control: Any,
    branch: str,
) -> None:
    gh = GitHub()
    gh.pr["head"]["ref"] = branch
    task = {"task_digest": "c" * 64}
    control.reject(gh, gh.pr, task, "Scope mismatch", "Expanded scope")
    control.reject(gh, gh.pr, task, "Scope mismatch", "Expanded scope")
    events = control.pending()
    assert len(events) == 1
    event = events[0]
    payload = json.loads(event["payload"])
    assert payload["notification_category"] == "review_rejection"
    assert payload["request_origin"] == {"branch": branch, "task_digest": task["task_digest"]}
    assert payload["head"] == gh.pr["head"]["sha"]
    assert payload["repository"] == gh.repository
    assert payload["number"] == gh.pr["number"]
    assert "no resume command" in payload["command"]
    assert event["token"] is None
    assert not control.paused(gh, gh.pr)
    assert control.correction(gh.repository, 1, gh.pr["head"]["sha"], task["task_digest"]) == {
        "reason": "Scope mismatch",
        "changes": "Expanded scope",
    }


def test_missing_branch_stays_unknown_and_does_not_use_current_enrollment(control: Any) -> None:
    gh = GitHub()
    branch = gh.pr["head"].pop("ref")
    control.enroll_feature(gh.repository, branch, "d" * 64)
    control.pause(gh, gh.pr, "Supply original evidence.", "Missing evidence")
    payload = json.loads(control.pending()[0]["payload"])
    assert payload["request_origin"] == {"branch": None, "task_digest": None}


def test_policy_notice_carries_assessed_digest_and_keeps_exact_head_callback(control: Any) -> None:
    gh = GitHub()
    gh.pr["head"]["ref"] = "codex/orchestrator-task"
    proto = controller.trusted_module("task_context")
    task = proto.repair_context("task", "author:test")
    digest = proto.digest(task)
    control.assess(gh.repository, 1, gh.pr["head"]["sha"], digest, "Repair trusted controller.")
    control.approval_notice(gh, gh.pr, [".github/agent/review_control.py"], task)
    event = control.pending()[0]
    payload = json.loads(event["payload"])
    assert payload["notification_category"] == "owner_approval"
    assert payload["request_origin"] == {
        "branch": gh.pr["head"]["ref"],
        "task_digest": digest,
    }
    assert payload["command"] == "/agent approve-policy " + gh.pr["head"]["sha"]
    assert event["token"]
    assert not control.approved(gh.repository, 1, gh.pr["head"]["sha"])
