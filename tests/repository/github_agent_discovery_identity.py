"""Fresh identities prevent denials against closed or superseded discovery targets."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.repository.github_agent import controller, run_refresh_discovery

__all__ = ["run_refresh_discovery"]


def single_target(fixture: Any, monkeypatch: Any) -> tuple[Any, list[Any]]:
    gh, state, _, _ = fixture
    state["runs"] = {}
    inventory = copy.deepcopy(gh.prs[0])
    gh.prs = [copy.deepcopy(inventory)]
    monkeypatch.setattr(gh, "pulls", lambda: [copy.deepcopy(inventory)])
    monkeypatch.setattr(gh, "pull", lambda number: copy.deepcopy(gh.prs[0]))
    posts: list[Any] = []
    api = gh.api

    def transport(path: str, method: str = "GET", data: Any = None) -> Any:
        if path == "check-runs":
            posts.append(data)
            return {}
        return api(path, method, data)

    monkeypatch.setattr(gh, "api", transport)
    return gh, posts


@pytest.mark.parametrize("state", ["closed", "merged"])
def test_closed_refresh_never_authorizes_or_selects_inventory_pr(
    run_refresh_discovery: Any,
    monkeypatch: Any,
    state: str,
) -> None:
    gh, posts = single_target(run_refresh_discovery, monkeypatch)
    gh.prs[0].update(state="closed", merged=state == "merged")
    signed: list[Any] = []
    monkeypatch.setattr(controller, "signed_head", lambda *args: signed.append(args))
    assert controller.discover(gh, 17) == {"include": []}
    assert not posts
    assert not signed


@pytest.mark.parametrize("change", ["closed", "head", "base", "body", "writer"])
def test_unsigned_observation_cannot_post_a_stale_denial(
    run_refresh_discovery: Any,
    monkeypatch: Any,
    change: str,
) -> None:
    gh, posts = single_target(run_refresh_discovery, monkeypatch)

    def unsigned(*args: Any) -> bool:
        if change == "closed":
            gh.prs[0]["state"] = "closed"
        elif change == "head":
            gh.prs[0]["head"]["sha"] = "c" * 40
        elif change == "base":
            gh._base = "c" * 40
        elif change == "body":
            gh.prs[0]["body"] = "New task context"
        else:
            gh.prs[0]["labels"] = [{"name": "agent:writing"}]
        return False

    monkeypatch.setattr(controller, "signed_head", unsigned)
    assert controller.discover(gh, 17) == {"include": []}
    assert not posts


def test_genuine_current_unsigned_failure_remains_visible(
    run_refresh_discovery: Any,
    monkeypatch: Any,
) -> None:
    gh, posts = single_target(run_refresh_discovery, monkeypatch)
    monkeypatch.setattr(controller, "signed_head", lambda *args: False)
    assert controller.discover(gh, 17) == {"include": []}
    assert len(posts) == 1
    assert posts[0]["conclusion"] == "action_required"


@pytest.mark.parametrize("boundary", ["metadata", "authorization"])
def test_closure_during_external_context_observation_cannot_reject(
    run_refresh_discovery: Any,
    monkeypatch: Any,
    boundary: str,
) -> None:
    gh, posts = single_target(run_refresh_discovery, monkeypatch)
    pauses: list[Any] = []
    monkeypatch.setattr(
        controller.trusted_module("review_control"), "pause", lambda *args: pauses.append(args)
    )

    def rejected(*args: Any) -> Any:
        gh.prs[0]["state"] = "closed"
        if boundary == "metadata":
            message = "Task provenance changed"
            raise ValueError(message)
        return "Enrollment changed"

    monkeypatch.setattr(
        controller, "task_metadata" if boundary == "metadata" else "authorization_error", rejected
    )
    assert controller.discover(gh, 17) == {"include": []}
    assert not posts
    assert not pauses


def test_closure_after_rejection_post_does_not_install_a_pause(
    run_refresh_discovery: Any,
    monkeypatch: Any,
) -> None:
    gh, posts = single_target(run_refresh_discovery, monkeypatch)
    pr = gh.pull(1)
    pauses: list[Any] = []
    monkeypatch.setattr(
        controller.trusted_module("review_control"), "pause", lambda *args: pauses.append(args)
    )
    api = gh.api

    def close_after_post(*args: Any) -> Any:
        result = api(*args)
        if args[0] == "check-runs":
            gh.prs[0]["state"] = "closed"
        return result

    monkeypatch.setattr(gh, "api", close_after_post)
    controller.rejection_check(gh, pr, gh.base(), "Actual rejection", "Actual changes")
    assert len(posts) == 1
    assert not pauses
