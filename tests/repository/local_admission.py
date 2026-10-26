"""Local review receipts admit PRs; Pi fixes pass only through the fraud check.

Real Git history drives the first-parent chain. GitHub, enrollment signatures
and receipt cryptography are faked at their external boundaries.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from tests.repository.github_agent import controller, owned_pr

APP = "analytics-toolkit-agent-717959[bot]"
CONTEXT = {
    "version": 1,
    "task_id": "admission-test",
    "source": "user",
    "producer_id": "session:author",
    "starting_prompt": "Implement the admission redesign.",
    "corrections": [],
}


def _git(root: Path, *args: str, committer: str = "Owner") -> str:
    email = committer.replace(" ", "") + "@example.invalid"
    environment = dict(
        os.environ,
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_AUTHOR_NAME=committer,
        GIT_AUTHOR_EMAIL=email,
        GIT_COMMITTER_NAME=committer,
        GIT_COMMITTER_EMAIL=email,
    )
    return subprocess.run(
        ["git", *args], cwd=root, env=environment, check=True, capture_output=True, text=True
    ).stdout.strip()


class History:
    """Real SSH-signed history: a developer key and the Pi controller key are enrolled."""

    def __init__(self, root: Path, keys: Path) -> None:
        self.root = root
        root.mkdir()
        keys.mkdir()
        self.keys = {name: keys / name for name in ("local", "pi")}
        lines = []
        for name, key in self.keys.items():
            _ssh_keygen("-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key))
            public = Path(str(key) + ".pub").read_text().strip()
            lines.append(name + ' namespaces="git" ' + public)
        self.allowed = keys / "allowed_signers"
        self.allowed.write_text("\n".join(lines) + "\n")
        _git(root, "init", "-q", "-b", "dev")
        self.trailers = controller.trusted_module("task_context").trailers(
            CONTEXT, CONTEXT["producer_id"]
        )
        self.base = self.commit("dev base", "base.py")

    def commit(
        self, message: str, path: str, *, committer: str = "Owner", key: str | None = "local"
    ) -> str:
        (self.root / path).write_text(message + "\n")
        _git(self.root, "add", path)
        options = ["commit", "-q"]
        if key:
            signing = ["-c", "gpg.format=ssh", "-c", "user.signingkey=" + str(self.keys[key])]
            options = [*signing, "commit", "-q", "-S"]
        _git(self.root, *options, "-m", message, committer=committer)
        return _git(self.root, "rev-parse", "HEAD")

    def owner(self, message: str = "Owner feature") -> str:
        return self.commit(message + self.trailers, "feature.py")

    def pi(self, kind: str = "correction", *, committer: str = APP, key: str | None = "pi") -> str:
        return self.commit(
            "GitHub agent: " + kind + self.trailers, kind + ".py", committer=committer, key=key
        )


def _ssh_keygen(*args: str) -> None:
    subprocess.run(["ssh-keygen", *args], check=True, capture_output=True)


class FakeGitHub:
    repository = "owner/repo"

    def __init__(self, pr: dict[str, Any], base: str) -> None:
        self.pr = pr
        self._base = base
        self.posted: list[dict[str, Any]] = []
        self.comments: list[str] = []
        self.files = [{"filename": "analytics_toolkit/feature.py"}]
        self.fast = "success"

    def base(self) -> str:
        return self._base

    def pulls(self) -> list[dict[str, Any]]:
        return [self.pr]

    def pull(self, number: int) -> dict[str, Any]:
        return self.pr

    def checks(self, head: str) -> list[dict[str, Any]]:
        review = [
            dict(check, app={"id": 17})
            for check in self.posted
            if check["head_sha"] == head and check["name"] == "agent-review"
        ]
        fast = [
            {
                "name": "fast-checks",
                "app": {"slug": "github-actions"},
                "conclusion": self.fast,
                "status": "completed",
            }
        ]
        return review[::-1] + fast

    def comment(self, number: int, body: str) -> None:
        self.comments.append(body)

    def pages(self, path: str, key: str | None = None) -> list[Any]:
        if "/files?" in path:
            return self.files
        if "/comments?" in path:
            return [{"body": body, "user": {"type": "Bot", "login": APP}} for body in self.comments]
        return []

    def api(self, path: str, method: str = "GET", data: Any = None) -> Any:
        route = path.split("?", 1)[0]
        if path == "collaborators/owner/permission":
            return {"permission": "admin"}
        if path.startswith("compare/"):
            return {"behind_by": 0}
        if route == "issues/1/labels":
            if method == "PUT":
                self.pr["labels"] = [{"name": name} for name in data["labels"]]
            return self.pr["labels"]
        assert (path, method) == ("check-runs", "POST")
        self.posted.append(data)
        return data


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    if not Path("/usr/bin/ssh-keygen").exists():
        pytest.skip("Host signature verification requires /usr/bin/ssh-keygen")
    history = History(tmp_path / "trusted", tmp_path / "keys")
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_CONTROL_DIR", str(history.root))
    monkeypatch.setenv("AGENT_ALLOWED_SIGNERS", str(history.allowed))
    monkeypatch.setenv("AGENT_SIGNING_KEY", str(history.keys["pi"]))
    monkeypatch.setenv("AGENT_APP_ID", "17")
    proto = controller.trusted_module("local_review")
    controls = controller.trusted_module("review_control")
    digest = controller.trusted_module("task_context").digest(CONTEXT)
    metadata = {"user_task": CONTEXT, "task_digest": digest, "producer_id": CONTEXT["producer_id"]}
    real_command = controller.command

    def command(*args: str, **kwargs: Any) -> Any:
        if args[:2] == ("git", "fetch"):
            return ""  # The trusted checkout already holds every object.
        return real_command(*args, **kwargs)

    monkeypatch.setattr(controller, "command", command)
    monkeypatch.setattr(proto, "verify_signature", Mock())
    monkeypatch.setattr(controller, "task_metadata", lambda *args: metadata)
    monkeypatch.setattr(controller, "authorization_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        controller, "monitor", lambda gh: (99, {"runs": {}, "started": "2026-10-01"})
    )
    monkeypatch.setattr(controller, "save_monitor", lambda *args: None)

    def receipt(head: str, **extra: Any) -> str:
        payload = {
            "version": 1,
            "kind": "local-source-review",
            "mode": "read-only",
            "repository": "owner/repo",
            "head": head,
            "base": history.base,
            "tree": _git(history.root, "rev-parse", head + "^{tree}"),
            "task_digest": digest,
            "producer_id": CONTEXT["producer_id"],
            "approved": True,
            "alignment": "aligned",
            "reviewer_id": "reviewer:12345678-1234-1234-1234-123456789abc",
            "execution_sha256": "e" * 64,
            "summary": "Local exact-source review.",
            **extra,
        }
        return proto.attachment({"payload": payload, "signature": "fixture"})

    def open_pr(head: str, body: str = "", *, bot: bool = False) -> FakeGitHub:
        pr = dict(owned_pr(), title="Admission", mergeable=True, labels=[], body=body)
        pr["head"] = dict(pr["head"], sha=head)
        if bot:
            pr["user"] = {"type": "Bot", "login": APP}
            pr["head"]["ref"] = controller.REPAIR_BRANCH + history.base[:12]
        return FakeGitHub(pr, history.base)

    return SimpleNamespace(
        history=history,
        receipt=receipt,
        open_pr=open_pr,
        controls=controls,
        metadata=metadata,
        digest=digest,
    )


def _assessed(case: Any) -> list[Any]:
    with closing(case.controls.connect()) as db:
        return db.execute("SELECT head, changes FROM assessments").fetchall()


def _verdict(decision: str) -> dict[str, Any]:
    findings = (
        []
        if decision == "accept"
        else [
            {"path": "tests/sql/read.py", "issue": "Removed the assertion on the returned frame."}
        ]
    )
    return {"decision": decision, "reason": "Checked the Pi fix commits.", "findings": findings}


def test_receipted_exact_head_is_admitted_with_reviewer_runtime(case: Any) -> None:
    head = case.history.owner()
    gh = case.open_pr(head, case.receipt(head, reviewer_runtime="claude-code"))
    assert controller.discover(gh, 17)["include"] == []
    assert [(c["conclusion"], c["output"]["title"]) for c in gh.posted] == [
        ("success", controller.ADMITTED)
    ]
    assert "(claude-code)" in gh.posted[0]["output"]["summary"]
    assert [row["head"] for row in _assessed(case)] == [head]
    assert controller.ready(gh.pr, gh.checks(head), gh.base(), 17)


def test_uncovered_head_is_action_required_and_never_loops(case: Any) -> None:
    gh = case.open_pr(case.history.owner())
    for _ in range(3):
        assert controller.discover(gh, 17)["include"] == []
    assert len(gh.posted) == 1
    assert gh.posted[0]["conclusion"] == "action_required"
    assert gh.posted[0]["output"]["title"] == controller.RECEIPT_REQUIRED
    assert case.controls.correction_snapshot(gh.repository, 1) is None
    assert not controller.ready(gh.pr, gh.checks(gh.pr["head"]["sha"]), gh.base(), 17)


@pytest.mark.parametrize(
    "laundering",
    ["owner-after-pi", "forged-pi-message", "unsigned-pi", "local-key-app-commit", "no-pi-key"],
)
def test_commits_after_the_receipt_cannot_launder_unreviewed_source(
    case: Any, monkeypatch: pytest.MonkeyPatch, laundering: str
) -> None:
    reviewed = case.history.owner()
    if laundering == "owner-after-pi":
        case.history.pi()
        head = case.history.owner("Unreviewed owner change")
    elif laundering == "forged-pi-message":
        head = case.history.pi(committer="Owner")
    elif laundering == "unsigned-pi":
        case.history.pi(key=None)
        head = case.history.pi("metadata")
    elif laundering == "local-key-app-commit":
        # An enrolled developer key, App committer name and Pi message: still not a Pi fix.
        head = case.history.pi(key="local")
    else:
        # Without the Pi controller key, genuine-looking Pi commits fail closed.
        head = case.history.pi()
        monkeypatch.delenv("AGENT_SIGNING_KEY")
    gh = case.open_pr(head, case.receipt(reviewed))
    for _ in range(2):
        assert controller.discover(gh, 17)["include"] == []
    assert [(c["conclusion"], c["output"]["title"]) for c in gh.posted] == [
        ("action_required", controller.RECEIPT_REQUIRED)
    ]
    assert _assessed(case) == []


def test_pi_fix_on_receipted_head_is_accepted_by_fraud_check(case: Any) -> None:
    reviewed = case.history.owner()
    fix = case.history.pi("conflict")
    gh = case.open_pr(fix, case.receipt(reviewed))
    tasks = controller.discover(gh, 17)["include"]
    assert [(t["kind"], t["anchor"], t["fix_commits"]) for t in tasks] == [
        ("fraud-review", reviewed, [fix])
    ]
    # Pending is in_progress (not failure); repeated discovery does not repost it.
    assert controller.discover(gh, 17)["include"] == tasks
    assert [(c["status"], c["output"]["title"]) for c in gh.posted] == [
        ("in_progress", controller.FRAUD_PENDING)
    ]
    assert controller.record_fraud_verdict(gh, tasks[0], _verdict("accept")) == {
        "fraud_review": "accepted"
    }
    assert gh.posted[-1]["conclusion"] == "success"
    assert gh.posted[-1]["output"]["title"] == controller.FRAUD_ACCEPTED
    assert controller.discover(gh, 17)["include"] == []
    assert controller.ready(gh.pr, gh.checks(fix), gh.base(), 17)
    with pytest.raises(RuntimeError, match="different fraud-review verdict"):
        controller.record_fraud_verdict(gh, tasks[0], _verdict("reject"))


def test_app_repair_is_admitted_only_after_fraud_accept(case: Any) -> None:
    repair = case.history.pi("repair")
    gh = case.open_pr(repair, bot=True)
    tasks = controller.discover(gh, 17)["include"]
    assert [(t["kind"], t["anchor"], t["fix_commits"]) for t in tasks] == [
        ("fraud-review", case.history.base, [repair])
    ]
    assert not controller.ready(gh.pr, gh.checks(repair), gh.base(), 17)
    controller.record_fraud_verdict(gh, tasks[0], _verdict("accept"))
    assert controller.ready(gh.pr, gh.checks(repair), gh.base(), 17)


def test_app_pr_with_a_non_pi_commit_requires_local_review(case: Any) -> None:
    case.history.pi("repair")
    head = case.history.owner("Hand-written change on the App branch")
    gh = case.open_pr(head, bot=True)
    assert controller.discover(gh, 17)["include"] == []
    assert gh.posted[0]["conclusion"] == "action_required"


def test_fraud_reject_queues_a_pi_correction_with_the_reason(case: Any) -> None:
    reviewed = case.history.owner()
    fix = case.history.pi("correction")
    gh = case.open_pr(fix, case.receipt(reviewed))
    task = controller.discover(gh, 17)["include"][0]
    assert controller.record_fraud_verdict(gh, task, _verdict("reject")) == {
        "fraud_review": "rejected"
    }
    assert gh.posted[-1]["conclusion"] == "action_required"  # Never failure.
    assert gh.posted[-1]["output"]["title"] == controller.FRAUD_REJECTED
    assert "Removed the assertion" in gh.comments[-1]
    correction = controller.discover(gh, 17)["include"]
    assert [t["kind"] for t in correction] == ["correction"]
    assert "Removed the assertion" in correction[0]["rejection"]["reason"]
    assert not controller.ready(gh.pr, gh.checks(fix), gh.base(), 17)


def test_stale_fraud_verdict_is_discarded(case: Any) -> None:
    reviewed = case.history.owner()
    fix = case.history.pi()
    gh = case.open_pr(fix, case.receipt(reviewed))
    task = controller.discover(gh, 17)["include"][0]
    with pytest.raises(RuntimeError, match="target changed"):
        controller.record_fraud_verdict(gh, dict(task, fix_commits=[reviewed]), _verdict("accept"))
    with pytest.raises(ValueError, match="Malformed"):
        controller.record_fraud_verdict(gh, task, {"decision": "accept", "reason": "ok"})
    with pytest.raises(ValueError, match="concrete findings"):
        controller.record_fraud_verdict(gh, task, dict(_verdict("reject"), findings=[]))
    assert gh.posted[-1]["output"]["title"] == controller.FRAUD_PENDING


@pytest.mark.parametrize("fast", ["in_progress", "failure", "success"])
def test_protected_approval_notice_is_sent_only_after_green_fast_ci(
    case: Any, monkeypatch: pytest.MonkeyPatch, fast: str
) -> None:
    head = case.history.owner()
    gh = case.open_pr(head, case.receipt(head))
    gh.files = [{"filename": ".github/agent/controller.py"}]
    gh.fast = fast
    monkeypatch.setattr(
        controller,
        "authorization_error",
        lambda gh, pr, require_policy=True, **kwargs: "Owner approval" if require_policy else None,
    )
    notices = Mock()
    monkeypatch.setattr(case.controls, "approval_notice", notices)
    for _ in range(2):
        controller.discover(gh, 17)
    if fast != "failure":  # Failed fast CI routes to a correction before admission.
        assert gh.posted[0]["output"]["title"] == controller.ADMITTED
    if fast == "success":
        notices.assert_called_once()
        assert notices.call_args.args[2] == [".github/agent/controller.py"]
    else:
        notices.assert_not_called()


@pytest.mark.parametrize(
    ("extra", "valid"),
    [
        ({}, True),
        ({"reviewer_runtime": "codex"}, True),
        ({"reviewer_runtime": "claude-code", "reviewer_model": "claude-opus"}, True),
        ({"reviewer_runtime": "gpt-cli"}, False),
        ({"reviewer_runtime": 1}, False),
        ({"reviewer_model": ["claude"]}, False),
    ],
)
def test_receipt_runtime_must_be_codex_or_claude_code(
    case: Any, extra: dict[str, Any], valid: bool
) -> None:
    head = case.history.owner()
    gh = case.open_pr(head, case.receipt(head, **extra))
    if valid:
        assert controller.admission(gh, gh.pr, gh.base(), case.metadata)["state"] == "admitted"
    else:
        with pytest.raises(ValueError, match="reviewer runtime"):
            controller.admission(gh, gh.pr, gh.base(), case.metadata)


def test_stale_reject_after_accept_never_queues_a_correction(case: Any) -> None:
    reviewed = case.history.owner()
    fix = case.history.pi()
    gh = case.open_pr(fix, case.receipt(reviewed))
    task = controller.discover(gh, 17)["include"][0]
    controller.record_fraud_verdict(gh, task, _verdict("accept"))
    with pytest.raises(RuntimeError, match="different fraud-review verdict"):
        controller.record_fraud_verdict(gh, task, _verdict("reject"))
    assert case.controls.correction_snapshot(gh.repository, 1) is None
    assert controller.discover(gh, 17)["include"] == []
    assert controller.ready(gh.pr, gh.checks(fix), gh.base(), 17)
