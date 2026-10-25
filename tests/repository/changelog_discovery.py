from __future__ import annotations

import base64
from typing import Any

import pytest

from tests.repository.github_agent import controller
from tests.repository.github_agent import (
    run_refresh_discovery as run_refresh_discovery,  # noqa: PLC0414 - pytest fixture
)


@pytest.mark.parametrize(
    "fragment",
    [b"Plain task summary\n", b"- First\n- Second\n", b"- First\rSecond", b"\xff", b""],
)
@pytest.mark.parametrize("behind", [0, 1])
def test_invalid_fragment_routes_to_fresh_correction_instead_of_retrying_metadata(
    run_refresh_discovery: Any, monkeypatch: Any, fragment: bytes, behind: int
) -> None:
    gh, _, _, metadata = run_refresh_discovery
    response = gh._response
    head = gh.pull(1)["head"]["sha"]

    def api(path: str, method: str) -> Any:
        if path.startswith("contents/"):
            assert path == f"contents/agent_tools/changelog/request.md?ref={head}"
            return {"content": base64.b64encode(fragment).decode()}
        if path.startswith("compare/"):
            return {"behind_by": behind}
        return response(path, method)

    monkeypatch.setattr(gh, "_response", api)
    result = controller.discover(gh, 17)["include"]
    task = next(task for task in result if task["number"] == 1)
    assert task["kind"] == "correction"
    assert task["head"] == head
    assert task["base"] == gh.base()
    assert "agent_tools/changelog/request.md" in task["rejection"]["reason"]
    assert "cannot be folded" in task["rejection"]["reason"]
    assert {key: task[key] for key in metadata} == metadata
    assert not any(task["kind"] == "metadata" for task in result)
    # A repeating discovery is idempotently imported under the same task/head identity.
    assert controller.discover(gh, 17)["include"] == result


def test_fragment_api_failure_does_not_invent_a_rejection_then_retries(
    run_refresh_discovery: Any,
) -> None:
    gh, _, _, _ = run_refresh_discovery
    path = f"contents/agent_tools/changelog/request.md?ref={gh.pull(1)['head']['sha']}"
    gh.errors[path] = RuntimeError("Transient GitHub read failure")
    with pytest.raises(RuntimeError, match="Transient GitHub read failure"):
        controller.discover(gh, 17)
    gh.errors.clear()
    assert controller.discover(gh, 17)["include"][0]["kind"] == "metadata"


@pytest.fixture
def local_receipt_discovery(
    run_refresh_discovery: Any,
    monkeypatch: Any,
    invalid: str | None,
) -> Any:
    gh, _, _, metadata = run_refresh_discovery
    pull = gh.pull
    proto = controller.trusted_module("local_review")
    old_head = pull(1)["head"]["sha"]
    original_message = controller.trusted_module("task_context").trailers(
        metadata["user_task"], metadata["producer_id"]
    )
    receipt = {
        "payload": {
            "version": 1,
            "kind": "local-source-review",
            "mode": "read-only",
            "repository": gh.repository,
            "head": old_head,
            "base": gh.base(),
            "tree": "tree",
            "task_digest": metadata["task_digest"],
            "producer_id": metadata["producer_id"],
            "reviewer_id": "reviewer:00000000-0000-0000-0000-000000000000",
            "approved": True,
            "alignment": "aligned",
            "execution_sha256": "a" * 64,
            "summary": "Locally approved",
        },
        "signature": "signature verified by host enrollment",
    }
    successor = []

    def locally_reviewed(number: int) -> Any:
        pr = pull(number)
        if number != 1:
            return pr
        return dict(
            pr,
            body=proto.attachment(receipt),
            head=dict(pr["head"], sha=successor[0] if successor else old_head),
        )

    monkeypatch.setattr(gh, "pull", locally_reviewed)
    verified = []

    def verify_signature(*args: Any) -> None:
        if invalid == "signature":
            message = "Invalid local review signature"
            raise ValueError(message)
        verified.append(args)

    monkeypatch.setattr(proto, "verify_signature", verify_signature)
    monkeypatch.setattr(
        controller,
        "signed_head",
        lambda repository, head: not (invalid == "unsigned" and successor and head == successor[0]),
    )
    monkeypatch.setenv("AGENT_CONTROL_DIR", "/trusted")
    monkeypatch.setenv("AGENT_ALLOWED_SIGNERS", "/trusted/signers")
    commands = []

    def command(*args: str, **kwargs: Any) -> str:
        commands.append(args)
        if args[:2] == ("git", "rev-parse"):
            assert args[2] == old_head + "^{tree}"
            return "tree"
        if args[:2] == ("git", "log"):
            assert args[-1] == old_head
            return (
                original_message.replace(receipt["payload"]["task_digest"], "f" * 64)
                if invalid == "provenance"
                else original_message
            )
        assert args == ("git", "merge-base", "--is-ancestor", old_head, successor[0])
        if invalid == "ancestry":
            message = "Reviewed commit is not an ancestor"
            raise RuntimeError(message)
        return ""

    monkeypatch.setattr(controller, "command", command)
    return gh, metadata, old_head, successor, verified, commands


@pytest.mark.parametrize("changed_producer", [False, True])
@pytest.mark.parametrize("enriched_task", [False, True])
@pytest.mark.parametrize("invalid", [None, "signature", "ancestry", "unsigned", "provenance"])
def test_local_receipt_cannot_skip_required_fragment_folding(
    local_receipt_discovery: Any,
    monkeypatch: Any,
    changed_producer: bool,
    enriched_task: bool,
    invalid: str | None,
) -> None:
    gh, metadata, old_head, successor, verified, commands = local_receipt_discovery
    task = next(item for item in controller.discover(gh, 17)["include"] if item["number"] == 1)
    assert task["kind"] == "metadata"
    assert not verified
    # Folding produces a new head. Its old local receipt must then yield a fresh Pi review.
    pages = gh.pages
    monkeypatch.setattr(
        gh,
        "pages",
        lambda path, key=None: (
            [dict(item, status="removed") for item in pages(path, key)]
            if path.startswith("pulls/1/")
            else pages(path, key)
        ),
    )
    successor.append("c" * 40)
    if changed_producer:
        metadata["producer_id"] = "author:independent-correction"
    if enriched_task:
        metadata["task_digest"] = "e" * 64
    if invalid:
        with pytest.raises((ValueError, RuntimeError)):
            controller.admit_local_review(gh, gh.pull(1), gh.base(), metadata)
        return
    task = next(item for item in controller.discover(gh, 17)["include"] if item["number"] == 1)
    assert task["kind"] == "review"
    assert task["head"] == successor[0]
    assert task["producer_id"] == metadata["producer_id"]
    assert task["task_digest"] == metadata["task_digest"]
    assert len(verified) == 1
    assert ("git", "merge-base", "--is-ancestor", old_head, successor[0]) in commands


def test_removed_fragment_is_not_read_or_recreated(
    run_refresh_discovery: Any, monkeypatch: Any
) -> None:
    gh, _, _, _ = run_refresh_discovery
    pages = gh.pages

    def files(path: str, key: str | None = None) -> Any:
        result = pages(path, key)
        if path.startswith("pulls/1/"):
            return [dict(item, status="removed") for item in result]
        return result

    monkeypatch.setattr(gh, "pages", files)
    assert controller.discover(gh, 17)["include"][0]["kind"] == "review"
    assert not any(path.startswith("contents/") for path in gh.calls)


def test_private_owner_hold_is_preserved_before_fragment_inspection(
    run_refresh_discovery: Any, monkeypatch: Any
) -> None:
    gh, _, _, _ = run_refresh_discovery
    controls = controller.trusted_module("review_control")
    monkeypatch.setattr(controls, "paused", lambda gh, pr: pr["number"] == 1)
    monkeypatch.setattr(controls, "reconcile_label", lambda *args, **kwargs: True)
    assert [task["number"] for task in controller.discover(gh, 17)["include"]] == [2]
    assert not any(path.startswith("contents/") for path in gh.calls)
