"""Incomplete review and terminal recovery exercise actual private state."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.repository.github_agent import checks, controller, owned_pr
from tests.repository.review_control import control as control  # noqa: PLC0414

role = controller.trusted_module("source_review")
TASK = {
    "kind": "review",
    "number": 1,
    "head": "a" * 40,
    "base": "b" * 40,
    "task_digest": "c" * 64,
    "producer_id": "author:independent-test",
}


def assignment(control: Any) -> dict[str, Any]:
    value = role.begin(control, "owner/repo", TASK)
    role.bind_bundle(
        control,
        "owner/repo",
        TASK,
        value["chain_id"],
        "d" * 64,
        {
            "source_paths": ["source.py"],
            "visual_pngs": [],
            "required_evidence": ["evidence"],
            "available_evidence": ["evidence"],
        },
    )
    return {
        "source_review_protocol": 1,
        "review_chain_id": value["chain_id"],
        "review_bundle_sha256": "d" * 64,
        "publisher_app_id": 17,
    }


class GitHub:
    repository = "owner/repo"

    def __init__(self, failure: str = "") -> None:
        self.failure = failure
        self.pr = dict(owned_pr(), mergeable=True, labels=[])
        self.checks_list: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self.comments: list[dict[str, Any]] = []

    def checks(self, head: str) -> list[dict[str, Any]]:
        assert head == TASK["head"]
        return self.checks_list

    def pages(self, path: str) -> list[dict[str, Any]]:
        return self.reviews if path.startswith("pulls/") else self.comments

    def api(self, path: str, method: str, data: dict[str, Any]) -> dict[str, Any]:
        assert method == "POST"
        if path == "check-runs":
            value = dict(data, id=1, app={"id": 17})
            self.checks_list.insert(0, value)
        else:
            assert path == "pulls/1/reviews"
            value = dict(data, user={"type": "Bot"})
            self.reviews.append(value)
        if self.failure == path:
            self.failure = ""
            message = "Accepted publication response lost"
            raise OSError(message)
        return value

    def comment(self, number: int, body: str) -> None:
        assert number == 1
        self.comments.append({"body": body, "user": {"type": "Bot"}})


def test_incomplete_and_legacy_results_cannot_invent_rejection() -> None:
    incomplete = {
        "source_review_protocol": 1,
        "completeness": "incomplete",
        "approved": False,
        "confirmed_findings": [],
        "unread_evidence": ["Source remains"],
    }
    assert role.disposition(incomplete, {"source_review_protocol": 1}) == "pending"
    assert role.disposition({"approved": False}, {}) == "pending"
    assert (
        role.disposition(
            dict(incomplete, source_review_protocol=True), {"source_review_protocol": 1}
        )
        == "pending"
    )
    incomplete["confirmed_findings"] = [
        {
            "location": "source.py:1",
            "requirement": "Retain state",
            "failure": "State is deleted",
            "correction": "Preserve state",
        }
    ]
    assert role.disposition(incomplete, {"source_review_protocol": 1}) == "reject"


def test_pending_blocks_merge_across_base_changes_and_replacement_is_cas(control: Any) -> None:
    receipt = assignment(control)
    pr = dict(owned_pr(), mergeable=True, labels=[])
    assert not controller.ready(pr, checks(TASK["base"]), TASK["base"], 17)
    assert not controller.ready(pr, checks("new-base"), "new-base", 17)
    replacement = dict(TASK, head="e" * 40)
    with pytest.raises(ValueError, match="changed"):
        role.begin(control, "owner/repo", replacement)
    assert role.verify_assignment(control, "owner/repo", TASK, receipt)


@pytest.mark.parametrize("outcome", ["approve", "reject"])
@pytest.mark.parametrize("failure", ["check-runs", "pulls/1/reviews", "correction", "notification"])
def test_terminal_publication_recovers_each_accepted_side_effect(
    control: Any,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    failure: str,
) -> None:
    receipt = assignment(control)
    gh = GitHub(failure)
    if outcome == "reject" and failure in {"correction", "notification"}:
        name = "require_correction" if failure == "correction" else "enqueue"
        original = getattr(control, name)
        raised = False

        def once(*args: Any, **kwargs: Any) -> Any:
            nonlocal raised
            result = original(*args, **kwargs)
            if not raised:
                raised = True
                message = "Accepted private side effect response lost"
                raise OSError(message)
            return result

        monkeypatch.setattr(control, name, once)
    expected_failure = failure in {"check-runs", "pulls/1/reviews"} or outcome == "reject"
    if expected_failure:
        with pytest.raises(OSError, match="response lost"):
            role.publish_terminal(
                control,
                gh,
                TASK,
                receipt,
                outcome,
                "Actual assessment",
                "Located defect",
                "Changes",
                lambda: gh.pr,
            )
        assert role.current(control, gh.repository, 1) is not None
    else:
        role.publish_terminal(
            control,
            gh,
            TASK,
            receipt,
            outcome,
            "Actual assessment",
            "Located defect",
            "Changes",
            lambda: gh.pr,
        )
        return
    role.publish_terminal(
        control,
        gh,
        TASK,
        receipt,
        outcome,
        "Actual assessment",
        "Located defect",
        "Changes",
        lambda: gh.pr,
    )
    assert role.current(control, gh.repository, 1) is None
    assert role.completed(control, gh.repository, TASK, receipt, outcome)
    assert len(gh.checks_list) == len(gh.reviews) == 1
    if outcome == "reject":
        assert len(gh.comments) == 1
        assert control.correction_snapshot(gh.repository, 1)["reason"] == "Located defect"


def test_required_evidence_growth_does_not_change_immutable_bundle(control: Any) -> None:
    value = role.begin(control, "owner/repo", TASK)
    inventory = {
        "source_paths": ["source.py"],
        "visual_pngs": [],
        "required_evidence": ["original"],
        "available_evidence": ["original", "discovered"],
    }
    arguments = (control, "owner/repo", TASK, value["chain_id"], "d" * 64)
    role.bind_bundle(*arguments, inventory)
    role.require_evidence(*arguments, ["discovered"])
    role.bind_bundle(*arguments, copy.deepcopy(inventory))
    current = role.current(control, "owner/repo", 1)
    assert current["inventory"] == inventory
    assert set(current["required_evidence"]) == {"original", "discovered"}


def test_generation_replacement_retries_exact_transition_without_retiring_barrier(
    control: Any,
) -> None:
    assignment(control)
    previous = role.current(control, "owner/repo", 1)
    changed = role.begin(
        control, "owner/repo", TASK, previous_sha256=role.digest(previous), transition_id="e" * 32
    )
    assert changed["chain_id"] != previous["chain_id"]
    assert (
        role.begin(
            control,
            "owner/repo",
            TASK,
            previous_sha256=role.digest(previous),
            transition_id="e" * 32,
        )
        == changed
    )
    with pytest.raises(ValueError, match="generation changed"):
        role.begin(
            control,
            "owner/repo",
            TASK,
            previous_sha256=role.digest(previous),
            transition_id="f" * 32,
        )
    assert role.blocks_merge(control, owned_pr(), TASK["base"])


def test_completed_review_does_not_clear_a_later_actual_correction(control: Any) -> None:
    receipt = assignment(control)
    role.finish(control, "owner/repo", TASK, receipt, "approve")
    assert control.require_correction(
        "owner/repo",
        1,
        TASK["head"],
        TASK["task_digest"],
        "New defect",
        "Preserve state",
        expected=None,
    )
    assert role.blocks_merge(control, owned_pr(), TASK["base"])


def test_feedback_supersession_requires_receipt_and_refuses_retired_transition(
    control: Any,
) -> None:
    receipt = assignment(control)
    role.finish(control, "owner/repo", TASK, receipt, "approve")
    with pytest.raises(ValueError, match="exact completed approval"):
        role.begin(control, "owner/repo", TASK, transition_id="e" * 32)
    with pytest.raises(ValueError, match="exact completed approval"):
        role.begin(
            control,
            "owner/repo",
            TASK,
            transition_id="e" * 32,
            completed_receipt=dict(receipt, publisher_app_id=99),
        )
    value = role.begin(
        control, "owner/repo", TASK, transition_id="e" * 32, completed_receipt=receipt
    )
    assert value["supersedes_completion"] == receipt["review_chain_id"]
    assert role.blocks_merge(control, owned_pr(), TASK["base"])
    role.bind_bundle(
        control,
        "owner/repo",
        TASK,
        value["chain_id"],
        "d" * 64,
        {
            "source_paths": ["source.py"],
            "visual_pngs": [],
            "required_evidence": ["evidence"],
            "available_evidence": ["evidence"],
        },
    )
    newer = dict(receipt, review_chain_id=value["chain_id"])
    role.finish(control, "owner/repo", TASK, newer, "approve")
    with pytest.raises(ValueError, match="exact completed approval"):
        role.begin(control, "owner/repo", TASK, transition_id="e" * 32, completed_receipt=receipt)


def test_terminal_recovery_preserves_later_real_findings(control: Any) -> None:
    receipt = assignment(control)
    gh = GitHub("check-runs")
    with pytest.raises(OSError, match="response lost"):
        role.publish_terminal(
            control,
            gh,
            TASK,
            receipt,
            "reject",
            "Actual review",
            "First finding",
            "Changes",
            lambda: gh.pr,
        )
    assert control.require_correction(
        gh.repository,
        1,
        TASK["head"],
        TASK["task_digest"],
        "Newer finding",
        "Newer changes",
        expected=None,
    )
    before = copy.deepcopy(control.correction_snapshot(gh.repository, 1))
    with pytest.raises(ValueError, match="Newer correction"):
        role.publish_terminal(
            control,
            gh,
            TASK,
            receipt,
            "reject",
            "Actual review",
            "First finding",
            "Changes",
            lambda: gh.pr,
        )
    assert control.correction_snapshot(gh.repository, 1) == before
    assert role.current(control, gh.repository, 1) is not None
