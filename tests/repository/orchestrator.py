from __future__ import annotations

import importlib.util

import pytest

from tests._support.paths import REPO_ROOT


def test_orchestrator_preserves_owner_request_and_valid_signed_context() -> None:
    spec = importlib.util.spec_from_file_location(
        "orchestrator_controller", REPO_ROOT / ".github/agent/controller.py"
    )
    assert spec is not None
    assert spec.loader is not None
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    context = controller.orchestrator_context("a" * 32)
    protocol = controller.trusted_module("task_context")
    assert protocol.validate(context) == context
    assert context["source"] == "user"
    assert "no more than 1 such agent" in context["starting_prompt"]
    assert "every 30 minutes" in context["starting_prompt"]
    assert "still no heavy checks on rpi" in context["starting_prompt"]
    assert context["corrections"] == [
        (
            "Either repository: an open PR in analytics_toolkit or self_host should "
            "trigger the 30-minute maintenance orchestrator.\n\nForwarded by Codex from "
            "the owner\u2019s actual chat answer, “Either repository (Recommended)”.\n\n"
            "Question: Should an open PR in either analytics_toolkit or self_host "
            "trigger the 30-minute orchestrator, or only an open analytics_toolkit PR?\n"
            "Answer: Either repository (Recommended)."
        ),
        (
            "Scope clarification from the owner: “PR in self_host is considered relevant "
            "only if it affects github agents functionality”. An open analytics_toolkit "
            "PR qualifies; an open self_host PR qualifies only when it affects GitHub "
            "agent functionality. Preserve one orchestrator globally and the 30-minute "
            "cadence.\n\nTransparently forwarded by Codex from "
            "the owner\u2019s actual latest "
            "chat message. This refines the earlier “Either repository (Recommended)” "
            "answer. It does not grant protected exact-head policy approval. For the "
            "remaining routine implementation options, use the previously requested "
            "independent plan resolver to check alignment with this scope.\n\nContext: "
            "https://github.com/Karapsin/analytics_toolkit/pull/26#issuecomment-6190878523"
        ),
    ]
    assert protocol.parse(protocol.render(context)) == context
    assert (
        protocol.verify_commit(context, "Maintenance" + protocol.trailers(context))
        == context["producer_id"]
    )
    assert controller.orchestrator_context("b" * 32)["task_id"] != context["task_id"]
    # A caller can append task-specific answers without changing future tasks.
    original_digest = protocol.digest(context)
    context["corrections"].append("Task-specific follow-up")
    fresh = controller.orchestrator_context("a" * 32)
    assert protocol.digest(fresh) == original_digest
    assert protocol.digest(context) != original_digest
    assert len(fresh["corrections"]) == 2
    with pytest.raises(ValueError, match="Invalid maintenance"):
        controller.orchestrator_context("../escape")
