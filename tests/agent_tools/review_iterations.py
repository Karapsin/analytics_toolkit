"""Independent novelty rounds converge durably without intermediate approval."""

from __future__ import annotations

import json
import uuid

import pytest

from agent_tools import bootstrap_process, local_review, review_iterations, sessions, task_context
from tests.agent_tools._support.local_review import signed_round
from tests.agent_tools.local_review_evidence import candidate as candidate  # noqa: PLC0414
from tests.agent_tools.local_review_evidence import commit
from tests.agent_tools.sessions import git
from tests.agent_tools.sessions import source as source  # noqa: PLC0414


def launch(monkeypatch, outcomes):
    calls = []

    def execute(root, job, subject, request):
        assert not (local_review.directory(root) / (subject["head"] + ".json")).exists()
        request = json.loads(request)
        calls.append(request)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        summary = outcome.pop("summary", None)
        receipt = signed_round(
            root,
            subject,
            prior_findings=request["iteration"]["prior_findings"],
            previous_rounds_sha256=request["iteration"]["previous_rounds_sha256"],
            **outcome,
        )
        if summary is not None:
            receipt["payload"]["verdict"]["summary"] = summary
            return local_review._sign_payload(root, job, receipt["payload"])
        return receipt

    monkeypatch.setattr(local_review, "_execute_review", execute)
    return calls


def unreviewed(candidate):
    head = commit(candidate, {"reviewed.py": "VALUE = 1\n"})
    return local_review.directory(candidate) / (head + ".json")


def test_first_clean_review_still_requires_a_second_fresh_novelty_search(candidate, monkeypatch):
    target = unreviewed(candidate)
    calls = launch(monkeypatch, [{}, {}])
    receipt = local_review.review(candidate)
    assert target.exists()
    assert [r["iteration"]["number"] for r in calls] == [1, 2]
    assert "NOT found" in calls[1]["iteration"]["instructions"]
    rounds = receipt["payload"]["review_iterations"]["rounds"]
    assert len({r["payload"]["reviewer_id"] for r in rounds}) == 2
    assert local_review.verified(candidate, receipt["payload"]["head"]) == receipt


def test_second_iteration_new_defect_never_leaves_first_approval_publishable(
    candidate, monkeypatch
):
    target = unreviewed(candidate)
    calls = launch(monkeypatch, [{}, {"findings": ["[P1] New source defect"], "approved": False}])
    with pytest.raises(RuntimeError, match="Local review rejected"):
        local_review.review(candidate)
    assert len(calls) == 2
    assert not target.exists()
    with pytest.raises(RuntimeError, match="Fresh independent"):
        local_review.verified(candidate, git(candidate, "rev-parse", "HEAD"))


def test_concrete_round_then_fixed_head_can_converge_in_second_completed_round(
    candidate, monkeypatch
):
    target = unreviewed(candidate)
    launch(monkeypatch, [{"findings": ["[P1] Original defect"], "approved": False}])
    with pytest.raises(RuntimeError, match="Local review rejected"):
        local_review.review(candidate)
    assert not target.exists()
    head = commit(candidate, {"reviewed.py": "VALUE = 2\n"})
    calls = launch(monkeypatch, [{}])
    receipt = local_review.review(candidate)
    assert calls[0]["iteration"]["number"] == 2
    assert calls[0]["iteration"]["prior_findings"][0]["findings"] == ["[P1] Original defect"]
    assert receipt["payload"]["head"] == head
    assert receipt["payload"]["review_iterations"]["completed_rounds"] == 2


def test_duplicate_unresolved_issue_is_not_a_clean_terminal_round(candidate, monkeypatch):
    target = unreviewed(candidate)
    for _ in range(2):
        launch(monkeypatch, [{"findings": ["[P1] Still unresolved"], "approved": False}])
        with pytest.raises(RuntimeError, match="Local review rejected"):
            local_review.review(candidate)
        assert not target.exists()
    calls = launch(monkeypatch, [{}])
    receipt = local_review.review(candidate)
    assert calls[0]["iteration"]["number"] == 3
    assert len(calls[0]["iteration"]["prior_findings"]) == 2
    assert receipt["payload"]["review_iterations"]["completed_rounds"] == 3


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), RuntimeError("interrupted")])
def test_interrupted_second_round_resumes_without_publishing_or_repeating_first(
    candidate, monkeypatch, failure
):
    target = unreviewed(candidate)
    launch(monkeypatch, [{}, failure])
    with pytest.raises(type(failure)):
        local_review.review(candidate)
    assert not target.exists()
    calls = launch(monkeypatch, [{}])
    local_review.review(candidate)
    assert [r["iteration"]["number"] for r in calls] == [2]


def test_incomplete_verdict_does_not_advance_the_pending_iteration(candidate, monkeypatch):
    target = unreviewed(candidate)
    launch(monkeypatch, [{"approved": False, "coverage": False}])
    with pytest.raises(
        RuntimeError, match="incomplete inspection or complete diff coverage missing"
    ):
        local_review.review(candidate)
    assert not target.exists()
    calls = launch(monkeypatch, [{}, {}])
    local_review.review(candidate)
    assert [r["iteration"]["number"] for r in calls] == [1, 2]


def test_request_requires_bounded_complete_inspection_but_still_blocks_incomplete_round(
    candidate, monkeypatch
):
    target = unreviewed(candidate)
    calls = launch(
        monkeypatch,
        [{"approved": False, "coverage": False, "summary": "Only five paths inspected"}],
    )
    with pytest.raises(RuntimeError, match="incomplete inspection"):
        local_review.review(candidate)
    assert len(calls) == 1
    request = calls[0]
    instructions = request["iteration"]["instructions"]
    assert "complete immutable inventory in bounded groups" in instructions
    assert "semantically assessing" in instructions
    assert "small initial sample or a size estimate while work can continue" in instructions
    assert "Concrete findings may still stop a round for repair" in instructions
    assert "tool failures, unresolved uncertainty" in instructions
    assert "still require rejection; never pressure approval or relax coverage" in instructions
    assert request["iteration"]["minimum_completed_rounds"] == 2
    assert request["user_task"] == task_context.protocol().public_context(
        task_context.read(candidate)
    )
    assert not target.exists()
    outcomes = [a["result"] for a in review_iterations.history(candidate)["attempts"]]
    assert [review_iterations.classification(r["payload"]) for r in outcomes] == ["incomplete"]


def test_concrete_issue_is_actionable_even_when_summary_describes_incomplete_scope(
    candidate, monkeypatch
):
    target = unreviewed(candidate)
    finding = "[P1] type_resolution.py::validate_profile accepts out-of-range date-only timestamps"
    launch(
        monkeypatch,
        [
            {
                "approved": False,
                "findings": [finding],
                "summary": "Only 10 paths assessed; incomplete",
            }
        ],
    )
    with pytest.raises(RuntimeError, match="concrete source issues require correction") as error:
        local_review.review(candidate)
    assert finding in str(error.value)
    assert str(error.value).index(finding) < str(error.value).index("Only 10 paths assessed")
    assert not target.exists()
    completed = [x["result"] for x in review_iterations.history(candidate)["attempts"]]
    assert [review_iterations.classification(x["payload"]) for x in completed] == ["issues"]


def test_approved_verdict_with_unresolved_findings_is_never_clean(candidate, monkeypatch):
    target = unreviewed(candidate)
    launch(monkeypatch, [{"findings": ["[P2] Unresolved despite approval"], "approved": True}])
    with pytest.raises(RuntimeError, match="Local review rejected"):
        local_review.review(candidate)
    assert not target.exists()


def test_reused_reviewer_identity_cannot_supply_the_second_round(candidate, monkeypatch):
    target = unreviewed(candidate)
    identity = "reviewer:" + str(uuid.uuid4())
    launch(monkeypatch, [{"reviewer_id": identity}, {"reviewer_id": identity}])
    with pytest.raises(ValueError, match="fresh reviewer identity"):
        local_review.review(candidate)
    assert not target.exists()


@pytest.mark.parametrize("change", ["task", "base"])
def test_changed_task_or_base_invalidates_round_cache(candidate, monkeypatch, change):
    unreviewed(candidate)
    launch(monkeypatch, [{}, RuntimeError("pause")])
    with pytest.raises(RuntimeError, match="pause"):
        local_review.review(candidate)
    if change == "task":
        task_context.record(candidate, "Additional task requirement", append=True)
        commit(candidate, {"reviewed.py": "VALUE = 3\n"})
    else:
        state = sessions.load(candidate)
        state["base"] = git(candidate, "rev-parse", "HEAD")
        sessions.save(candidate, state)
    calls = launch(monkeypatch, [{}, {}])
    local_review.review(candidate)
    assert [r["iteration"]["number"] for r in calls] == [1, 2]


def test_signed_round_history_survives_restart_and_detects_tampering(candidate, monkeypatch):
    unreviewed(candidate)
    launch(monkeypatch, [{}, RuntimeError("pause")])
    with pytest.raises(RuntimeError, match="pause"):
        local_review.review(candidate)
    history = review_iterations.history(candidate)
    result = next(x["result"] for x in history["attempts"] if "result" in x)
    result["payload"]["verdict"]["summary"] = "Tampered"
    review_iterations._save(candidate, history)
    with pytest.raises(ValueError, match="signature"):
        review_iterations.history(candidate)


def test_concurrent_invocation_cannot_launch_another_first_round(candidate, monkeypatch):
    unreviewed(candidate)
    calls = launch(monkeypatch, [{}, {}])
    lock_path = local_review.directory(candidate) / "iterations.lock"
    with bootstrap_process.lock(lock_path, wait=False), pytest.raises(
        RuntimeError, match="already running"
    ):
        local_review.review(candidate)
    assert calls == []
    local_review.review(candidate)
    assert [x["iteration"]["number"] for x in calls] == [1, 2]


def test_completed_terminal_round_survives_interruption_before_final_receipt(
    candidate, monkeypatch
):
    target = unreviewed(candidate)
    count = 0

    def execute(root, job, subject, request):
        nonlocal count
        count += 1
        iteration = json.loads(request)["iteration"]
        receipt = signed_round(
            root,
            subject,
            prior_findings=iteration["prior_findings"],
            previous_rounds_sha256=iteration["previous_rounds_sha256"],
        )
        review_iterations.record(root, iteration["attempt_id"], receipt)
        if count == 2:
            raise KeyboardInterrupt
        return receipt

    monkeypatch.setattr(local_review, "_execute_review", execute)
    with pytest.raises(KeyboardInterrupt):
        local_review.review(candidate)
    assert not target.exists()
    calls = launch(monkeypatch, [])
    receipt = local_review.review(candidate)
    assert calls == []
    assert receipt["payload"]["review_iterations"]["completed_rounds"] == 2
