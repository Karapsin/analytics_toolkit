"""Signed local admission coexists with durable independent source review."""

from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from tests.repository.github_agent import checks, controller, owned_pr

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_CONTROL_DIR", str(tmp_path / "trusted"))
    monkeypatch.setenv("AGENT_ALLOWED_SIGNERS", str(tmp_path / "signers"))
    controls = controller.trusted_module("review_control")
    role = controller.trusted_module("source_review")
    proto = controller.trusted_module("local_review")
    task = {
        "kind": "review",
        "number": 1,
        "head": "a" * 40,
        "base": "b" * 40,
        "task_digest": "c" * 64,
        "producer_id": "session:author",
    }
    metadata = {
        "producer_id": task["producer_id"],
        "task_digest": task["task_digest"],
        "user_task": {},
        "local_review_required": True,
    }
    receipt = {
        "payload": {
            "version": 1,
            "kind": "local-source-review",
            "mode": "read-only",
            "repository": "owner/repo",
            "head": task["head"],
            "base": task["base"],
            "tree": "d" * 40,
            "task_digest": task["task_digest"],
            "producer_id": task["producer_id"],
            "approved": True,
            "alignment": "aligned",
            "reviewer_id": "reviewer:12345678-1234-1234-1234-123456789abc",
            "execution_sha256": "e" * 64,
            "summary": "Independent exact-source review.",
        },
        "signature": "external signature fixture",
    }
    pr = dict(owned_pr(), title="Resumable review", mergeable=True, labels=[])
    pr["body"] = proto.attachment(receipt)
    gh = Mock(repository="owner/repo")
    gh.base.return_value = task["base"]
    gh.pulls.return_value = [pr]
    gh.pull.return_value = pr
    gh.pages.return_value = []
    gh.checks.return_value = [checks(task["base"])[1]]

    def api(path: str, method: str = "GET", data: Any = None) -> Any:
        if path == "collaborators/owner/permission":
            return {"permission": "admin"}
        if path.startswith("compare/"):
            return {"behind_by": 0}
        if path.split("?", 1)[0] == "issues/1/labels":
            if method == "PUT":
                pr["labels"] = [{"name": name} for name in data["labels"]]
            else:
                assert method == "GET"
            return pr["labels"]
        assert path == "check-runs"
        assert method == "POST"
        return data

    gh.api.side_effect = api
    # Fake external Git, enrollment and cryptography only; parse/bind real receipts.
    verify = Mock()
    monkeypatch.setattr(proto, "verify_signature", verify)
    monkeypatch.setattr(controller, "command", Mock(return_value="d" * 40))
    monkeypatch.setattr(controller, "signed_head", lambda *args: True)
    monkeypatch.setattr(controller, "authorization_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(controller, "task_metadata", lambda *args: metadata)
    monkeypatch.setattr(
        controller, "monitor", lambda gh: (99, {"runs": {}, "started": "2026-10-01T00:00:00Z"})
    )
    monkeypatch.setattr(controller, "save_monitor", lambda *args: None)
    assess = Mock(wraps=controls.assess)
    monkeypatch.setattr(controls, "assess", assess)
    return SimpleNamespace(
        gh=gh,
        pr=pr,
        task=task,
        metadata=metadata,
        receipt=receipt,
        controls=controls,
        role=role,
        proto=proto,
        verify=verify,
        assess=assess,
        signers=tmp_path / "signers",
    )


def _assessments(case: Any) -> list[Any]:
    with closing(case.controls.connect()) as db:
        return db.execute("SELECT * FROM assessments").fetchall()


@pytest.mark.parametrize("assignment", ["none", "matching", "old-head", "old-base"])
def test_authenticated_local_receipt_never_overrides_active_assignment(
    case: Any, assignment: str
) -> None:
    previous = None
    if assignment != "none":
        task = dict(case.task)
        if assignment == "old-head":
            task["head"] = "f" * 40
        elif assignment == "old-base":
            task["base"] = "f" * 40
        previous = case.role.begin(case.controls, case.gh.repository, task)
    admitted = controller.admit_local_review(case.gh, case.pr, case.task["base"], case.metadata)
    assert admitted is (assignment == "none")
    case.verify.assert_called_once_with(case.receipt, case.signers)
    assert case.role.current(case.controls, case.gh.repository, 1) == previous
    if previous is None:
        case.assess.assert_called_once()
        assert len(_assessments(case)) == 1
        posted = case.gh.api.call_args.args
        assert posted[0:2] == ("check-runs", "POST")
        assert posted[2]["conclusion"] == "success"
    else:
        case.assess.assert_not_called()
        assert _assessments(case) == []
        assert not any(call.args[0] == "check-runs" for call in case.gh.api.call_args_list)
        assert not controller.ready(case.pr, checks(case.task["base"]), case.task["base"], 17)


def test_discovery_admits_valid_local_receipt_without_active_assignment(case: Any) -> None:
    assert controller.discover(case.gh, 17)["include"] == []
    case.verify.assert_called_once_with(case.receipt, case.signers)
    case.assess.assert_called_once()
    assert len(_assessments(case)) == 1
    assert case.role.current(case.controls, case.gh.repository, 1) is None
    posted = [call.args[2] for call in case.gh.api.call_args_list if call.args[0] == "check-runs"]
    assert len(posted) == 1
    assert posted[0]["conclusion"] == "success"


@pytest.mark.parametrize("conclusion", ["failure", "success", "action_required", None])
def test_pending_review_repeated_discovery_preserves_assignment_without_correction(
    case: Any, conclusion: str | None
) -> None:
    previous = case.role.begin(case.controls, case.gh.repository, case.task)
    review = dict(
        checks(case.task["base"])[0],
        status="in_progress" if conclusion is None else "completed",
        conclusion=conclusion,
    )
    case.gh.checks.return_value.insert(0, review)
    for _ in range(2):
        tasks = controller.discover(case.gh, 17)["include"]
        assert len(tasks) == 1
        assert tasks[0]["kind"] == "review"
        assert "rejection" not in tasks[0]
        assert case.role.current(case.controls, case.gh.repository, 1) == previous
        assert case.controls.correction_snapshot(case.gh.repository, 1) is None
    case.assess.assert_not_called()
    assert _assessments(case) == []
    assert not any(call.args[0] == "check-runs" for call in case.gh.api.call_args_list)


@pytest.mark.parametrize("failure", ["failure", "cancelled", "timed_out", "correction"])
def test_pending_review_does_not_hide_ci_failures_or_concrete_corrections(
    case: Any, failure: str
) -> None:
    previous = case.role.begin(case.controls, case.gh.repository, case.task)
    if failure == "correction":
        assert case.controls.require_correction(
            case.gh.repository,
            1,
            case.task["head"],
            case.task["task_digest"],
            "Concrete source defect.",
            "Repair the confirmed defect.",
            expected=None,
        )
    else:
        case.gh.checks.return_value[0]["conclusion"] = failure
    correction = case.controls.correction_snapshot(case.gh.repository, 1)
    case.gh.checks.return_value.insert(
        0, dict(checks(case.task["base"])[0], status="completed", conclusion="failure")
    )
    for _ in range(2):
        tasks = controller.discover(case.gh, 17)["include"]
        assert len(tasks) == 1
        assert tasks[0]["kind"] == "correction"
        reason = tasks[0]["rejection"]["reason"]
        if failure == "correction":
            assert reason == "Concrete source defect."
        else:
            assert "Fast CI failed" in reason
        assert case.controls.correction_snapshot(case.gh.repository, 1) == correction
        assert case.role.current(case.controls, case.gh.repository, 1) == previous
    case.assess.assert_not_called()


@pytest.mark.parametrize("invalid", ["malformed", "missing-required", "binding", "signature"])
def test_pending_assignment_does_not_bypass_receipt_provenance(case: Any, invalid: str) -> None:
    previous = case.role.begin(case.controls, case.gh.repository, case.task)
    if invalid == "malformed":
        case.pr["body"] = "<!-- atk-local-review:v1 invalid -->"
        message = "Malformed local review receipt"
    elif invalid == "missing-required":
        case.pr["body"] = ""
        message = "requires a matching receipt"
    elif invalid == "binding":
        case.receipt["payload"]["task_digest"] = "f" * 64
        case.pr["body"] = case.proto.attachment(case.receipt)
        message = "does not approve this exact source"
    else:
        case.verify.side_effect = ValueError("Invalid local review signature")
        message = "Invalid local review signature"
    with pytest.raises(ValueError, match=message):
        controller.admit_local_review(case.gh, case.pr, case.task["base"], case.metadata)
    assert case.role.current(case.controls, case.gh.repository, 1) == previous
    case.assess.assert_not_called()
    assert _assessments(case) == []
    assert not any(call.args[0] == "check-runs" for call in case.gh.api.call_args_list)
