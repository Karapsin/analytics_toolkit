from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from tests.repository.github_agent import controller, owned_pr

if TYPE_CHECKING:
    from pathlib import Path

evidence = controller.trusted_module("repair_evidence")


def _discovery_with_inventory_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    status: int,
) -> tuple[Any, ...]:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    metadata = {
        "id": 7,
        "run_attempt": 1,
        "head_sha": "old",
        "run_started_at": "2026-10-01",
        "status": "completed",
        "conclusion": "failure",
        "html_url": "run",
        "event": "workflow_dispatch",
        "updated_at": "2026-10-02",
    }
    state = {
        "issue": 99,
        "started": "2026-10-01",
        "runs": {
            "7": {
                "sha": "old",
                "status": "completed",
                "conclusion": "failure",
                "attempt": 1,
                "needs": ["HTTP"],
                "green": [],
                "retry_after": 123,
            }
        },
    }
    base = "b" * 40
    pulls = []
    for number in range(1, 5):
        pr = owned_pr()
        pr["number"] = number
        pr["head"]["sha"] = str(number) * 40
        pr.update(mergeable=number != 3, labels=[])
        pulls.append(pr)

    gh = Mock(repository="owner/repo")
    gh.base.return_value = base
    gh.pulls.return_value = pulls
    gh.pull.side_effect = lambda number: pulls[number - 1]
    gh.checks.return_value = []
    failure = {"active": True}
    metadata_calls = 0

    def api(path: str, method: str = "GET", data: Any = None) -> Any:
        nonlocal metadata_calls
        if path == "actions/runs/7":
            metadata_calls += 1
            # discover first refreshes the tracked run; the second call is inventory.
            if failure["active"] and endpoint == "metadata" and metadata_calls == 2:
                message = f"gh api failed: unavailable (HTTP {status})"
                raise RuntimeError(message)
            return metadata
        if path.startswith("collaborators/"):
            return {"permission": "admin"}
        if path.startswith("compare/"):
            return {"behind_by": 0}
        assert path == "issues/99" and method == "PATCH"
        return None

    def pages(path: str, key: str | None = None) -> list[Any]:
        if "/artifacts?" in path:
            assert key == "artifacts"
            if failure["active"] and endpoint == "artifacts":
                message = f"gh api failed: unavailable (HTTP {status})"
                raise RuntimeError(message)
            return []
        if path == "pulls/4/files?per_page=100":
            return [{"filename": "agent_tools/changelog/four.md"}]
        return []

    gh.api.side_effect = api
    gh.pages.side_effect = pages
    controls = Mock()
    controls.paused.return_value = False
    controls.correction.side_effect = lambda repository, number, head, digest: (
        {"reason": "fix", "changes": "fix"} if number == 2 else None
    )
    original_module = controller.trusted_module
    monkeypatch.setattr(
        controller,
        "trusted_module",
        lambda name: controls if name == "review_control" else original_module(name),
    )
    monkeypatch.setattr(controller, "monitor", lambda gh: (99, state))
    monkeypatch.setattr(controller, "signed_head", lambda *args: True)
    monkeypatch.setattr(controller, "authorization_error", lambda *args: None)
    monkeypatch.setattr(
        controller,
        "task_metadata",
        lambda gh, pr: {"task_digest": "digest", "producer_id": "owner", "user_task": {}},
    )
    blocked = Mock(wraps=evidence.blocked)
    record = Mock(wraps=evidence.record)
    monkeypatch.setattr(evidence, "blocked", blocked)
    monkeypatch.setattr(evidence, "record", record)

    return gh, state, controls, blocked, record, failure


@pytest.mark.parametrize("endpoint", ["metadata", "artifacts"])
@pytest.mark.parametrize("status", [500, 503])
def test_transient_inventory_preserves_pr_tasks_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    status: int,
) -> None:
    gh, state, controls, blocked, record, failure = _discovery_with_inventory_failure(
        tmp_path, monkeypatch, endpoint, status
    )
    tasks = controller.discover(gh, 17)["include"]
    assert [(task["number"], task["kind"]) for task in tasks] == [
        (1, "review"),
        (2, "correction"),
        (3, "conflict"),
        (4, "metadata"),
    ]
    persisted = controller._stored_state()
    assert persisted is not None
    assert persisted["cursor"] == state["cursor"]
    assert persisted["runs"]["7"]["needs"] == ["HTTP"]
    assert persisted["runs"]["7"]["retry_after"] == 123
    blocked.assert_not_called()
    record.assert_not_called()
    controls.enqueue.assert_not_called()
    gh.comment.assert_not_called()
    assert not (tmp_path / "repair-outcomes.sqlite3").exists()

    failure["active"] = False
    retried = controller.discover(gh, 17)["include"]
    assert retried[0]["kind"] == "repair"
    assert retried[1:] == tasks
    assert retried[0]["evidence_key"] == evidence.fingerprint(evidence.inventory(gh, [7]))
    assert state["runs"]["7"]["needs"] == ["HTTP"]
    assert state["runs"]["7"]["retry_after"] == 123
    record.assert_not_called()
    gh.comment.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("gh api failed: denied (HTTP 401)"),
        RuntimeError("gh api failed: denied (HTTP 403)"),
        RuntimeError("other tool failed: unavailable (HTTP 503)"),
        RuntimeError("gh api failed: HTTP 503"),
        ValueError("Malformed inventory"),
    ],
)
def test_inventory_non_transient_errors_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    state = {"runs": {"7": {"sha": "old", "status": "completed", "needs": ["HTTP"]}}}
    before = copy.deepcopy(state)
    gh = Mock()
    gh.api.side_effect = error
    with pytest.raises(type(error)) as caught:
        controller._integration_tasks(gh, state, "a" * 40, [])
    assert caught.value is error
    assert state == before


def test_malformed_inventory_metadata_fails_closed() -> None:
    gh = Mock()
    gh.api.return_value = {}
    gh.pages.return_value = []
    state = {"runs": {"7": {"sha": "old", "status": "completed", "needs": ["HTTP"]}}}
    with pytest.raises(KeyError, match="run_attempt"):
        controller._integration_tasks(gh, state, "a" * 40, [])


@pytest.mark.parametrize("boundary", ["fingerprint", "blocked"])
def test_transient_looking_errors_outside_inventory_are_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    monkeypatch.setattr(evidence, "inventory", lambda gh, runs: [])
    monkeypatch.setattr(
        evidence,
        boundary,
        Mock(side_effect=RuntimeError("gh api failed: unavailable (HTTP 503)")),
    )
    state = {"runs": {"7": {"sha": "old", "status": "completed", "needs": ["HTTP"]}}}
    with pytest.raises(RuntimeError, match="unavailable"):
        controller._integration_tasks(object(), state, "a" * 40, [])
