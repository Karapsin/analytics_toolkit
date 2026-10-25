"""Controller admission of signed local review and workflow-scoped validation."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.repository.github_agent import controller


@pytest.mark.parametrize("base", ["base", "advanced"])
def test_local_approval_verifies_signature_and_only_certifies_reviewed_base(monkeypatch, base):
    payload = {"base": "base", "head": "head", "summary": "Reviewed", "reviewer_id": "reviewer:id"}
    receipt = {"payload": payload, "signature": "signed"}
    proto = SimpleNamespace(
        extract=Mock(return_value=receipt),
        validate_binding=Mock(return_value=payload),
        verify_signature=Mock(),
        digest=lambda value: "digest",
    )
    controls = SimpleNamespace(assess=Mock())
    modules = {
        "local_review": proto,
        "review_control": controls,
        "source_review": SimpleNamespace(current=Mock(return_value=None)),
    }
    monkeypatch.setattr(controller, "trusted_module", modules.__getitem__)
    monkeypatch.setenv("AGENT_CONTROL_DIR", "/trusted")
    monkeypatch.setenv("AGENT_ALLOWED_SIGNERS", "/trusted/signers")
    monkeypatch.setattr(controller, "command", lambda *args, **kwargs: "tree")
    monkeypatch.setattr(controller, "_current_discovery_target", lambda *args: True)
    gh = SimpleNamespace(repository="owner/repo", api=Mock())
    pr = {"number": 9, "body": "receipt", "head": {"sha": "head"}}
    metadata = {"producer_id": "session:author", "task_digest": "task"}
    assert controller.admit_local_review(gh, pr, base, metadata) is (base == "base")
    proto.verify_signature.assert_called_once()
    assert gh.api.call_count == (1 if base == "base" else 0)


def test_missing_required_or_malformed_receipt_cannot_become_approval(monkeypatch):
    proto = SimpleNamespace(extract=lambda body: None)
    monkeypatch.setattr(controller, "trusted_module", lambda name: proto)
    assert controller.admit_local_review(None, {}, "base", {}) is False
    with pytest.raises(ValueError, match="requires a matching"):
        controller.admit_local_review(None, {}, "base", {"local_review_required": True})


@pytest.mark.parametrize("payload", [None, [], "invalid", 1])
def test_malformed_payload_is_a_per_pr_provenance_failure(monkeypatch, payload):
    proto = SimpleNamespace(extract=lambda body: {"payload": payload})
    monkeypatch.setattr(controller, "trusted_module", lambda name: proto)
    with pytest.raises(ValueError, match="Malformed local review payload"):
        controller.admit_local_review(None, {"head": {"sha": "head"}}, "base", {})


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
