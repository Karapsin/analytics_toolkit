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
    assert protocol.parse(protocol.render(context)) == context
    assert (
        protocol.verify_commit(context, "Maintenance" + protocol.trailers(context))
        == context["producer_id"]
    )
    assert controller.orchestrator_context("b" * 32)["task_id"] != context["task_id"]
    with pytest.raises(ValueError, match="Invalid maintenance"):
        controller.orchestrator_context("../escape")
