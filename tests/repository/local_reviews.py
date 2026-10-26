"""Controller admission of signed local review and workflow-scoped validation."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.repository.github_agent import controller


@pytest.mark.parametrize(
    ("base", "ancestor"), [("b" * 40, True), ("c" * 40, True), ("c" * 40, False)]
)
def test_local_approval_verifies_signature_and_admits_only_covered_bases(
    monkeypatch, base, ancestor
):
    payload = {"base": "b" * 40, "head": "a" * 40, "summary": "Reviewed"}
    payload["reviewer_id"] = "reviewer:id"
    receipt = {"payload": payload, "signature": "signed"}
    proto = SimpleNamespace(
        extract=Mock(return_value=receipt),
        validate_binding=Mock(return_value=payload),
        verify_signature=Mock(),
        digest=lambda value: "digest",
    )
    monkeypatch.setattr(controller, "trusted_module", {"local_review": proto}.__getitem__)
    monkeypatch.setenv("AGENT_CONTROL_DIR", "/trusted")
    monkeypatch.setenv("AGENT_ALLOWED_SIGNERS", "/trusted/signers")

    def command(*args, **kwargs):
        if args[:3] == ("git", "merge-base", "--is-ancestor") and not ancestor:
            message = "not an ancestor"
            raise RuntimeError(message)
        return "tree"

    monkeypatch.setattr(controller, "command", command)
    pr = {"number": 9, "body": "receipt", "head": {"sha": "a" * 40}, "user": {"type": "User"}}
    metadata = {"producer_id": "session:author", "task_digest": "task", "user_task": {}}
    result = controller.admission(SimpleNamespace(repository="owner/repo"), pr, base, metadata)
    assert result["state"] == ("admitted" if ancestor else "required")
    proto.verify_signature.assert_called_once()
    if ancestor:
        assert result["reviewer"] == "reviewer:id (codex)"


def test_missing_receipt_requires_local_review_and_required_policy_is_provenance(monkeypatch):
    proto = SimpleNamespace(extract=lambda body: None)
    monkeypatch.setattr(controller, "trusted_module", lambda name: proto)
    pr = {"head": {"sha": "a" * 40}, "user": {"type": "User"}}
    assert controller.admission(None, pr, "base", {})["state"] == "required"
    with pytest.raises(ValueError, match="requires a matching"):
        controller.admission(None, pr, "base", {"local_review_required": True})


@pytest.mark.parametrize("payload", [None, [], "invalid", 1])
def test_malformed_payload_is_a_per_pr_provenance_failure(monkeypatch, payload):
    proto = SimpleNamespace(extract=lambda body: {"payload": payload})
    monkeypatch.setattr(controller, "trusted_module", lambda name: proto)
    with pytest.raises(ValueError, match="Malformed local review payload"):
        controller.admission(None, {"head": {"sha": "head"}, "user": {"type": "User"}}, "base", {})


def test_same_job_name_in_another_workflow_cannot_resolve_failure():
    failure = {
        "workflow": ".github/workflows/sql-integration.yml",
        "sha": "same",
        "status": "completed",
        "needs": ["shared job"],
        "green": [],
        "updated": "1",
    }
    green = {
        "workflow": ".github/workflows/compatibility.yml",
        "sha": "same",
        "status": "completed",
        "needs": [],
        "green": ["shared job"],
        "updated": "2",
        "url": "green",
    }
    state = {"runs": {"1": deepcopy(failure), "2": green}}
    controller._resolve_runs(None, state)
    assert state["runs"]["1"]["needs"] == ["shared job"]
    green["workflow"] = failure["workflow"]
    controller._resolve_runs(None, state)
    assert state["runs"]["1"]["needs"] == []
