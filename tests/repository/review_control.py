from __future__ import annotations

import base64
import copy
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock

import pytest

from tests.repository.github_agent import controller, owned_pr


@pytest.fixture
def control(tmp_path: Any, monkeypatch: Any) -> Any:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    return controller.trusted_module("review_control")


class GitHub:
    repository = "owner/repo"

    def __init__(self) -> None:
        self.pr = owned_pr()
        self.pr.update(title="Change export scope", body="", labels=[])
        self.comments: list[dict[str, Any]] = []

    def pages(self, path: str) -> list[dict[str, Any]]:
        return self.comments

    def comment(self, number: int, body: str) -> None:
        self.comments.append({"body": body, "user": {"type": "Bot", "login": "app"}})

    def api(self, path: str, method: str = "GET", data: Any = None) -> Any:
        if path.endswith("/labels") and method == "POST":
            self.pr["labels"] = [{"name": x} for x in data["labels"]]
        if method == "DELETE":
            self.pr["labels"] = []
        return self.pr["labels"]

    def pull(self, number: int) -> dict[str, Any]:
        return self.pr


def test_rejection_is_durable_head_changes_do_not_resume_and_alert_has_no_buttons(
    control: Any,
) -> None:
    gh = GitHub()
    control.pause(gh, gh.pr, "Prompt allows CSV; patch adds SQL writes.", "Added database writes")
    assert control.paused(gh, gh.pr)
    gh.pr["head"]["sha"] = "b" * 40
    assert control.paused(gh, gh.pr)
    events = control.pending()
    assert len(events) == 1
    assert events[0]["token"] is None
    gh.comments.append(
        {
            "body": "/agent resume " + "b" * 40,
            "user": {"type": "User", "login": "owner"},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    assert not control.paused(gh, gh.pr)


def test_callback_requires_owner_channel_message_and_current_head(control: Any) -> None:
    gh = GitHub()
    proto = controller.trusted_module("task_context")
    task = proto.repair_context("task", "author:test")
    control.assess(
        gh.repository,
        1,
        gh.pr["head"]["sha"],
        proto.digest(task),
        "Protect signed PR review and owner approval.",
    )
    control.approval_notice(gh, gh.pr, [".github/agent/controller.py"], task)
    event = control.pending()[0]
    control.delivery(event["key"], message_id=7)
    query = {
        "id": "callback-1",
        "data": "approve:" + event["token"],
        "from": {"id": 22},
        "message": {"message_id": 7, "chat": {"id": -1001}},
    }
    with pytest.raises(ValueError, match="configured Telegram owner"):
        control.callback(gh, query, 33, -1001)
    with pytest.raises(ValueError, match="configured Telegram owner"):
        control.callback(gh, query, 22, -1002)
    gh.pr["head"]["sha"] = "b" * 40
    with pytest.raises(ValueError, match="PR changed"):
        control.callback(gh, query, 22, -1001)
    gh.pr["head"]["sha"] = "a" * 40
    assert "separate review" in control.callback(gh, query, 22, -1001)
    assert control.approved(gh.repository, 1, "a" * 40)
    assert not control.approved(gh.repository, 1, "b" * 40)
    assert "/agent approve-policy " + "a" * 40 in gh.comments[0]["body"]
    query["data"] = "disapprove:" + event["token"]
    query["id"] = "callback-2"
    control.callback(gh, query, 22, -1001)
    assert not control.approved(gh.repository, 1, "a" * 40)
    assert control.paused(gh, gh.pr)


def test_retry_outbox_deduplicates_event_and_keeps_actionable_explanation(control: Any) -> None:
    control.enqueue("one", "rejection", {"changes": "Expanded scope", "reason": "Not requested"})
    control.enqueue("one", "rejection", {"changes": "Duplicate"})
    event = control.pending()[0]
    assert json.loads(event["payload"])["reason"] == "Not requested"
    control.delivery("one", delay=30)
    assert not control.pending()
    control.delivery("one", message_id=10)
    assert not control.pending()


@pytest.mark.parametrize(
    "path",
    [
        "AGENTS.md",
        "agent_docs/sql.md",
        "release_routines/sql_integration.py",
        ".github/workflows/sql-integration.yml",
        "agent_tools/sessions.py",
    ],
)
def test_normal_policy_and_test_repairs_no_longer_require_owner_comment(path: str) -> None:
    assert not controller.policy_path(path)


@pytest.mark.parametrize(
    "path",
    [
        ".github/agent/prompts/review.md",
        ".github/required-workflows.json",
        ".github/workflows/agent-dispatch.yml",
        ".codex/config.toml",
    ],
)
def test_controller_security_paths_still_require_owner_comment(path: str) -> None:
    assert controller.policy_path(path)


def test_review_execution_must_be_fresh_read_only_and_bound_to_task() -> None:
    task = {
        "kind": "review",
        "head": "a" * 40,
        "base": "b" * 40,
        "task_digest": "c" * 64,
        "producer_id": "author:one",
    }
    receipt = {**task, "mode": "read-only", "reviewer_id": "reviewer:two"}
    controller.verify_review_execution(task, receipt)
    for changed in (
        {"head": "d" * 40},
        {"task_digest": "e" * 64},
        {"mode": "workspace-write"},
        {"reviewer_id": "author:one"},
    ):
        with pytest.raises(RuntimeError):
            controller.verify_review_execution(task, {**receipt, **changed})


def test_app_features_need_host_enrollment_and_exact_task_context(control: Any) -> None:
    gh = GitHub()
    pr = copy.deepcopy(gh.pr)
    pr["user"] = {"type": "Bot", "login": "analytics-toolkit-agent-717959[bot]"}
    pr["head"]["ref"] = "codex/telegram-feature-owner-task"
    assert not controller.trusted(pr, gh.repository)
    control.enroll_feature(gh.repository, pr["head"]["ref"], "a" * 64)
    assert controller.trusted(pr, gh.repository)
    assert control.enrolled_feature(gh.repository, pr["head"]["ref"], "a" * 64)
    assert not control.enrolled_feature(gh.repository, pr["head"]["ref"], "b" * 64)
    other = copy.deepcopy(pr)
    other["head"]["ref"] = "codex/telegram-feature-attacker"
    assert not controller.trusted(other, gh.repository)


def test_owner_correction_holds_existing_pr_before_replanning(control: Any) -> None:
    gh = GitHub()
    gh.pr["head"]["ref"] = "codex/telegram-feature-task"
    control.enroll_feature(gh.repository, gh.pr["head"]["ref"], "a" * 64)
    control.hold_feature(gh, gh.pr)
    assert control.paused(gh, gh.pr)
    assert not control.pending()  # A user correction is not a reviewer rejection.
    control.resume_feature(gh, gh.pr)
    assert not control.paused(gh, gh.pr)


def test_publication_recovery_does_not_clear_a_real_reviewer_rejection(control: Any) -> None:
    gh = GitHub()
    gh.pr["head"]["ref"] = "codex/telegram-feature-task"
    control.enroll_feature(gh.repository, gh.pr["head"]["ref"], "a" * 64)
    control.pause(gh, gh.pr, "Scope does not match the user request.", "Unrequested API")
    control.recover_feature_publication(gh, gh.pr)
    assert control.paused(gh, gh.pr)
    with control.connect() as db:
        db.execute(
            "UPDATE pauses SET reason=?",
            ("PR task context is not bound to its signed head commit.",),
        )
    control.recover_feature_publication(gh, gh.pr)
    assert not control.paused(gh, gh.pr)


def test_success_notifications_require_real_merge_promotion_and_production_publish(
    control: Any,
) -> None:
    gh = Mock(repository="owner/repo")
    main = ["a" * 40]
    promotion = ["diverged"]
    pr = {
        "number": 5,
        "title": "CSV export",
        "head": {"sha": "b" * 40},
        "merge_commit_sha": "c" * 40,
        "merged_at": "2026-10-12T10:00:00Z",
        "html_url": "https://github.com/owner/repo/pull/5",
    }
    run = {
        "id": 8,
        "created_at": "2026-10-12T11:00:00Z",
        "head_sha": "d" * 40,
        "html_url": "https://github.com/owner/repo/actions/runs/8",
    }
    jobs = [{"name": "publish to TestPyPI", "conclusion": "success"}]

    def api(path: str) -> Any:
        if path == "git/ref/heads/main":
            return {"object": {"sha": main[0]}}
        if path.startswith("compare/"):
            return {"status": promotion[0], "commits": [{"commit": {"message": "CSV export"}}]}
        if path.startswith("commits/"):
            return {"sha": "d" * 40}
        return {
            "content": base64.b64encode(
                b"## 1.2.3.4 - 2026-10-12\n\n- Added CSV export.\n"
            ).decode()
        }

    def pages(path: str, key: str | None = None) -> Any:
        if path.startswith("pulls?"):
            return [pr]
        if path.startswith("actions/workflows/"):
            return [run]
        if path.endswith("/jobs?per_page=100"):
            return jobs
        return [
            {
                "tag_name": "v1.2.3.4",
                "published_at": "2026-10-12T11:01:00Z",
                "draft": False,
                "html_url": "https://github.com/owner/repo/releases/tag/v1.2.3.4",
            }
        ]

    gh.api.side_effect = api
    gh.pages.side_effect = pages
    gh.base.return_value = "e" * 40
    gh.checks.return_value = [
        {
            "name": "agent-review",
            "conclusion": "success",
            "app": {"id": 20},
            "output": {"summary": "What changed: Added CSV export.\n\nApproved"},
        }
    ]
    control.set_cursor("owner/repo:notifications:start", "2026-10-01T00:00:00Z")
    control.set_cursor("owner/repo:notifications:main", "f" * 40)
    control.observe(gh, 20)
    assert [event["kind"] for event in control.pending()] == ["merged"]
    main[0] = "9" * 40
    promotion[0] = "identical"
    jobs[0]["name"] = "publish to PyPI"
    control.observe(gh, 20)
    control.observe(gh, 20)
    events = control.pending()
    assert [event["kind"] for event in events] == ["merged", "main", "release"]
    assert "Added CSV export" in json.loads(events[-1]["payload"])["changes"]
    assert all(event["token"] is None for event in events)
