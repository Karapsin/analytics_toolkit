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


def test_local_receipt_cannot_skip_required_fragment_folding(
    run_refresh_discovery: Any, monkeypatch: Any
) -> None:
    gh, _, _, _ = run_refresh_discovery
    pull = gh.pull

    def locally_reviewed(number: int) -> Any:
        return dict(pull(number), body="<!-- atk-local-review:v1 signed receipt -->")

    monkeypatch.setattr(gh, "pull", locally_reviewed)
    admitted = []
    monkeypatch.setattr(controller, "admit_local_review", lambda *args: admitted.append(args))
    task = next(item for item in controller.discover(gh, 17)["include"] if item["number"] == 1)
    assert task["kind"] == "metadata"
    assert not any(args[1]["number"] == 1 for args in admitted)
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
    monkeypatch.setattr(controller, "admit_local_review", lambda *args: False)
    task = next(item for item in controller.discover(gh, 17)["include"] if item["number"] == 1)
    assert task["kind"] == "review"


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
