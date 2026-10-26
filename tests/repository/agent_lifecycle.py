from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from tests.repository.github_agent import controller
from tests.repository.review_control import GitHub

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def control(tmp_path: Path, monkeypatch: Any) -> Any:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    return controller.trusted_module("review_control")


@pytest.mark.parametrize(
    "reason",
    [
        "Feature context does not match the host-owned request.",
        "PR task context is not bound to its signed head commit.",
        "Supply the original image.",
        "Owner cancelled the request.",
        "Owner disapproved the controller/security change via Telegram.",
    ],
)
@pytest.mark.parametrize("changed_head", [False, True])
def test_factual_answer_cannot_replace_existing_authority_hold(
    control: Any,
    reason: str,
    changed_head: bool,
) -> None:
    gh = GitHub()
    branch = gh.pr["head"]["ref"]
    control.enroll_feature(gh.repository, branch, "original-digest")
    with control.connect() as db:
        db.execute(
            "INSERT INTO pauses VALUES (?,?,?,?,?)",
            (gh.repository, gh.pr["number"], gh.pr["head"]["sha"], reason, 1),
        )
        original = tuple(db.execute("SELECT * FROM pauses").fetchone())
    if changed_head:
        gh.pr["head"]["sha"] = "b" * 40

    # This is the real controller entrypoint used by authenticated Telegram
    # replies, including factual answers to mixed coordinator/account questions.
    control.hold_feature(gh, gh.pr)
    restarted = controller.trusted_module("review_control")
    restarted.hold_feature(gh, gh.pr)
    assert restarted.paused(gh, gh.pr)
    with restarted.connect() as db:
        assert tuple(db.execute("SELECT * FROM pauses").fetchone()) == original
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    assert not restarted.pending()


@pytest.mark.parametrize("orchestrator", [False, True])
@pytest.mark.parametrize("replacement", [None, "image", "cancel", "same-reason", "new-head"])
def test_publication_recovery_cannot_delete_a_replacement_pause(
    control: Any,
    monkeypatch: Any,
    orchestrator: bool,
    replacement: str | None,
) -> None:
    gh = GitHub()
    gh.pr["head"]["ref"] = (
        "codex/orchestrator-task" if orchestrator else "codex/telegram-feature-task"
    )
    digest = "a" * 64
    control.enroll_feature(gh.repository, gh.pr["head"]["ref"], digest)
    reason = (
        "Feature context does not match the host-owned request."
        if orchestrator
        else "PR task context is not bound to its signed head commit."
    )
    with control.connect() as db:
        db.execute(
            "INSERT INTO pauses VALUES (?,?,?,?,?)",
            (gh.repository, gh.pr["number"], gh.pr["head"]["sha"], reason, 1000),
        )
    enroll = control.enrolled_feature
    expected: tuple[Any, ...] | None = None

    def replace_during_verification(*args: Any) -> bool:
        nonlocal expected
        enrolled = enroll(*args)
        if replacement is not None:
            replacement_reason = {
                "image": "Supply the original image.",
                "cancel": "Owner cancelled the request.",
            }.get(replacement, reason)
            head = "b" * 40 if replacement == "new-head" else gh.pr["head"]["sha"]
            with control.connect() as db:
                db.execute(
                    "UPDATE pauses SET head=?,reason=?,created=?",
                    (head, replacement_reason, 2000),
                )
                expected = tuple(db.execute("SELECT * FROM pauses").fetchone())
        return enrolled

    # Deterministic interleaving after the pause read and before its deletion.
    # No worker, sleeps, network or external services are needed.
    monkeypatch.setattr(control, "enrolled_feature", replace_during_verification)
    control.recover_feature_publication(gh, gh.pr, task_digest=digest if orchestrator else None)
    monkeypatch.delitem(controller.MODULES, "review_control")
    restarted = controller.trusted_module("review_control")
    with restarted.connect() as db:
        row = db.execute("SELECT * FROM pauses").fetchone()
        assert (tuple(row) if row is not None else None) == expected
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    assert restarted.paused(gh, gh.pr) == (replacement is not None)
    if replacement in {"image", "cancel"}:
        restarted.recover_feature_publication(
            gh, gh.pr, task_digest=digest if orchestrator else None
        )
        with restarted.connect() as db:
            assert tuple(db.execute("SELECT * FROM pauses").fetchone()) == expected


def _discovery_client(gh: Any, monkeypatch: Any) -> tuple[Any, Any]:
    client = Mock(repository=gh.repository)
    client.pull.side_effect = gh.pull
    client.pulls.return_value = [gh.pr]
    client.base.return_value = "base"
    client.checks.return_value = []
    client.pages.side_effect = lambda path, *args: gh.comments if "/comments?" in path else []
    client.api.side_effect = lambda path, *args: (
        {"permission": "admin"}
        if path.endswith("/permission")
        else {"behind_by": 0}
        if path.startswith("compare/")
        else gh.api(path, *args)
    )
    monkeypatch.setattr(controller, "monitor", lambda client: (None, {"started": "", "runs": {}}))
    monkeypatch.setattr(controller, "save_monitor", lambda *args: None)
    monkeypatch.setattr(controller, "_integration_tasks", lambda *args: [])
    monkeypatch.setattr(controller, "signed_head", lambda *args: True)
    metadata = Mock(return_value={"task_digest": "digest"})
    monkeypatch.setattr(controller, "task_metadata", metadata)
    monkeypatch.setattr(controller, "authorization_error", lambda *args, recover=True: None)
    return client, metadata


class _PauseReadConnection:
    """Wrap only the pause read, leaving actual SQLite writes unchanged."""

    def __init__(self, db: Any, cursor_type: Any) -> None:
        self.db = db
        self.cursor_type = cursor_type

    def execute(self, sql: str, parameters: Any = ()) -> Any:
        cursor = self.db.execute(sql, parameters)
        if sql.startswith("SELECT") and "FROM pauses" in sql:
            return self.cursor_type(cursor)
        return cursor


@pytest.mark.parametrize("replacement", ["image", "cancel", "same-reason", "new-head"])
def test_correction_recovery_preserves_concurrent_hold_after_restart(
    control: Any,
    monkeypatch: Any,
    tmp_path: Path,
    replacement: str,
) -> None:
    gh = GitHub()
    control.hold_correction(gh, gh.pr)
    reason = {
        "image": "Supply the original image.",
        "cancel": "Owner cancelled the correction.",
    }.get(replacement, "Correction agent requires user input.")
    head = "b" * 40 if replacement == "new-head" else gh.pr["head"]["sha"]
    expected = (gh.repository, gh.pr["number"], head, reason, 2000)
    real_connect = control.connect
    attempted = False
    deferred = False

    def replace_hold() -> None:
        # A real second SQLite connection, with no wait or background worker.
        writer = sqlite3.connect(tmp_path / "review_control.sqlite3", timeout=0)
        try:
            with writer:
                writer.execute("INSERT OR REPLACE INTO pauses VALUES (?,?,?,?,?)", expected)
        finally:
            writer.close()

    class Cursor:
        def __init__(self, cursor: Any) -> None:
            self.cursor = cursor

        def fetchone(self) -> Any:
            nonlocal attempted, deferred
            row = self.cursor.fetchone()
            self.cursor.fetchall()  # Exhaust the single-row read before the other writer.
            if not attempted:
                attempted = True
                try:
                    replace_hold()
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error):
                        raise
                    # The protected transaction wins this ordering. The writer
                    # retries immediately after commit, before label cleanup.
                    deferred = True
            return row

    @contextmanager
    def interleaved_connect() -> Any:
        db = real_connect()
        try:
            with db:
                yield _PauseReadConnection(db, Cursor)
            if deferred:
                replace_hold()
        finally:
            db.close()

    # On the old implementation the writer succeeds after SELECT, then the
    # broad DELETE erases its replacement. This hook fixes the interleaving
    # without timing assumptions, threads, sleeps or external services.
    with monkeypatch.context() as patch:
        patch.setattr(control, "connect", interleaved_connect)
        control.resume_correction(gh, gh.pr)
    assert attempted
    monkeypatch.delitem(controller.MODULES, "review_control")
    restarted = controller.trusted_module("review_control")
    with restarted.connect() as db:
        assert tuple(db.execute("SELECT * FROM pauses").fetchone()) == expected
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    assert restarted.paused(gh, gh.pr)
    assert control.PAUSED in {label["name"] for label in gh.pr["labels"]}

    client, metadata = _discovery_client(gh, monkeypatch)
    assert controller.discover(client, 17) == {"include": []}
    monkeypatch.setattr(controller, "ready", lambda *args: True)
    controller.merge_selected(client, 17, tmp_path, pr_numbers={gh.pr["number"]})
    metadata.assert_not_called()
    assert not any(call.args[0].endswith("/merge") for call in client.api.call_args_list)


@pytest.mark.parametrize(
    "unrelated",
    [None, "Supply the original image.", "Owner disapproved protected changes."],
)
@pytest.mark.parametrize("changed_head", [False, True])
def test_new_amendment_rejects_stale_resume_after_restart_and_discovery(
    control: Any,
    monkeypatch: Any,
    tmp_path: Path,
    changed_head: bool,
    unrelated: str | None,
) -> None:
    gh = GitHub()
    control.enroll_feature(gh.repository, gh.pr["head"]["ref"], "original-digest")
    monkeypatch.setattr(control.time, "time", lambda: 1000)
    if unrelated is None:
        control.hold_feature(gh, gh.pr)
    else:
        with control.connect() as db:
            db.execute(
                "INSERT INTO pauses VALUES (?,?,?,?,?)",
                (gh.repository, gh.pr["number"], gh.pr["head"]["sha"], unrelated, 1000),
            )
    if changed_head:
        gh.pr["head"]["sha"] = "b" * 40
    gh.comments.append(
        {
            "body": "/agent resume " + gh.pr["head"]["sha"],
            "user": {"type": "User", "login": "owner"},
            "created_at": datetime.fromtimestamp(2000, timezone.utc).isoformat(),
        }
    )
    monkeypatch.setattr(control.time, "time", lambda: 3000)
    control.hold_feature(gh, gh.pr)
    if unrelated is not None:
        with control.connect() as db:
            row = dict(db.execute("SELECT * FROM pauses").fetchone())
            assert (row["reason"], row["created"]) == (unrelated, 1000)
            assert db.execute("SELECT created FROM feature_amendments").fetchone()[0] == 3000
    monkeypatch.delitem(controller.MODULES, "review_control")
    restarted = controller.trusted_module("review_control")
    assert restarted.paused(gh, gh.pr)
    with restarted.connect() as db:
        pause = dict(db.execute("SELECT * FROM pauses").fetchone())
        assert pause["created"] == 3000
        assert pause["head"] == gh.pr["head"]["sha"]
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0

    # Exercise discovery with the real pause parser and label reconciliation.
    # Only GitHub, signatures and unrelated integration bookkeeping are faked.
    client, metadata = _discovery_client(gh, monkeypatch)
    assert controller.discover(client, 17) == {"include": []}
    metadata.assert_not_called()
    assert restarted.paused(gh, gh.pr)
    monkeypatch.setattr(controller, "ready", lambda *args: True)
    controller.merge(client, 17, tmp_path)
    metadata.assert_not_called()
    assert not any(call.args[0].endswith("/merge") for call in client.api.call_args_list)

    # A later authenticated exact-head resume still works; it grants no approval.
    gh.comments.append(
        {
            **gh.comments[0],
            "created_at": datetime.fromtimestamp(4000, timezone.utc).isoformat(),
        }
    )
    assert not restarted.paused(gh, gh.pr)
    # Resumed work is admitted only by a local receipt; without one it awaits it.
    assert controller.discover(client, 17)["include"] == []
    posted = [call.args for call in client.api.call_args_list if call.args[0] == "check-runs"]
    assert posted[-1][2]["output"]["title"] == controller.RECEIPT_REQUIRED
    assert posted[-1][2]["conclusion"] == "action_required"
    assert not restarted.approved(gh.repository, 1, gh.pr["head"]["sha"])


def test_policy_decision_cannot_release_a_newer_pending_amendment(
    control: Any, monkeypatch: Any
) -> None:
    gh = GitHub()
    control.enroll_feature(gh.repository, gh.pr["head"]["ref"], "original-digest")
    with control.connect() as db:
        db.execute(
            "INSERT INTO pauses VALUES (?,?,?,?,?)",
            (
                gh.repository,
                gh.pr["number"],
                gh.pr["head"]["sha"],
                "Owner disapproved policy.",
                1000,
            ),
        )
    gh.comments.append(
        {
            "body": "/agent approve-policy " + gh.pr["head"]["sha"],
            "user": {"type": "User", "login": "owner"},
            "created_at": datetime.fromtimestamp(2000, timezone.utc).isoformat(),
        }
    )
    monkeypatch.setattr(control.time, "time", lambda: 3000)
    control.hold_feature(gh, gh.pr)
    assert control.paused(gh, gh.pr)
    with control.connect() as db:
        assert db.execute("SELECT created FROM pauses").fetchone()[0] == 3000
    assert not control.approved(gh.repository, gh.pr["number"], gh.pr["head"]["sha"])


def test_unheld_answer_still_requires_replanning_and_cancel_still_closes(
    control: Any,
    monkeypatch: Any,
) -> None:
    gh = GitHub()
    control.enroll_feature(gh.repository, gh.pr["head"]["ref"], "original-digest")
    control.hold_feature(gh, gh.pr)
    with control.connect() as db:
        assert db.execute("SELECT reason FROM pauses").fetchone()[0] == (
            "Owner changed the request; renewed planning is required."
        )

    api = Mock(wraps=gh.api)
    monkeypatch.setattr(gh, "api", api)
    control.hold_feature(gh, gh.pr, cancel=True)
    api.assert_any_call(f"pulls/{gh.pr['number']}", "PATCH", {"state": "closed"})
    control.hold_feature(gh, gh.pr)
    with control.connect() as db:
        assert (
            db.execute("SELECT reason FROM pauses").fetchone()[0] == "Owner cancelled the request."
        )
    assert control.paused(gh, gh.pr)


def test_unenrolled_answer_cannot_create_or_replace_a_hold(control: Any) -> None:
    gh = GitHub()
    with pytest.raises(ValueError, match="not host-enrolled"):
        control.hold_feature(gh, gh.pr)
    with control.connect() as db:
        assert db.execute("SELECT count(*) FROM pauses").fetchone()[0] == 0


@pytest.mark.parametrize("replacement", [None, "same-reason", "provenance", "veto", "review"])
def test_publication_capability_retains_exact_amendment_for_discovery_and_merge(
    control: Any,
    monkeypatch: Any,
    tmp_path: Path,
    replacement: str | None,
) -> None:
    gh = GitHub()
    control.enroll_feature(gh.repository, gh.pr["head"]["ref"], "new-digest")
    control.hold_feature(gh, gh.pr)
    with control.connect() as db:
        snapshot = dict(db.execute("SELECT * FROM pauses").fetchone())
    client, metadata = _discovery_client(gh, monkeypatch)
    task = {
        "kind": "feature",
        "number": gh.pr["number"],
        "head": gh.pr["head"]["sha"],
        "base": "base",
        "branch": gh.pr["head"]["ref"],
        "task_digest": "new-digest",
        "previous_task_digest": "digest",
    }
    with pytest.raises(RuntimeError, match="awaiting user instructions"):
        controller.publication_authority(client, task)
    assert controller.publication_authority(client, task, renewal_hold=snapshot) == gh.pr
    # Publication permission never releases review/merge or supplies approval.
    assert control.paused(client, gh.pr)
    metadata.reset_mock()
    assert controller.discover(client, 17) == {"include": []}
    monkeypatch.setattr(controller, "ready", lambda *args: True)
    controller.merge_selected(client, 17, tmp_path, pr_numbers={gh.pr["number"]})
    metadata.assert_not_called()
    assert not any(call.args[0].endswith("/merge") for call in client.api.call_args_list)
    if replacement is None:
        return
    with control.connect() as db:
        if replacement in {"same-reason", "provenance"}:
            db.execute("UPDATE pauses SET created=created+1")
            if replacement == "provenance":
                db.execute("UPDATE pauses SET reason='Supply the original image.'")
        elif replacement == "veto":
            db.execute(
                "INSERT INTO vetoes VALUES (?,?,?,?)",
                (gh.repository, gh.pr["number"], task["head"], 1),
            )
        else:
            task["kind"] = "review"
        original = tuple(db.execute("SELECT * FROM pauses").fetchone())
    with pytest.raises(RuntimeError, match="amendment authority changed"):
        controller.publication_authority(client, task, renewal_hold=snapshot)
    with control.connect() as db:
        assert tuple(db.execute("SELECT * FROM pauses").fetchone()) == original
        assert db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


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


def test_correction_plan_receipt_is_optional_but_binds_when_supplied(tmp_path: Path) -> None:
    task = {
        "head": "a" * 40,
        "base": "b" * 40,
        "task_digest": "c" * 64,
        "plan_hash": "d" * 64,
        "producer_id": "author:one",
    }
    # Plan review is retired: no receipt means the Pi decided the correction itself.
    controller.verify_correction_plan(task, tmp_path)
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
        with pytest.raises(RuntimeError, match="legacy correction plan receipt"):
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
