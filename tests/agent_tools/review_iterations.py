"""Independent novelty rounds converge durably without intermediate approval."""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from agent_tools import (
    bootstrap_process,
    local_review,
    local_review_files,
    review_chunks,
    review_iterations,
    sessions,
    task_context,
)
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
        outcome = dict(outcome)
        summary = outcome.pop("summary", None)
        recorded = request["iteration"]["recorded_findings"]
        receipt = signed_round(
            root,
            subject,
            modern=True,
            review_chunk=request["review_chunk"],
            discovery_only=request["iteration"]["discovery_only"],
            recorded_findings=recorded,
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


def finding_id(finding):
    return "finding:" + hashlib.sha256(finding.encode()).hexdigest()


def current_results(root, previous):
    """Select new exact-subject attempts without discarding the signed audit prefix."""
    subject = local_review.binding(root, git(root, "rev-parse", "HEAD"))
    attempts = review_iterations.history(root)["attempts"]
    assert previous
    assert all(attempt["subject"] != subject for attempt in previous)
    assert attempts[: len(previous)] == previous
    current = attempts[len(previous) :]
    assert current
    assert all(attempt["subject"] == subject for attempt in current)
    return [attempt["result"] for attempt in current]


def receipt_waves(root, receipt):
    digest = receipt["payload"]["review_iterations"]["proof_sha256"]
    return json.loads((local_review.directory(root) / ("proof-" + digest + ".json")).read_text())[
        "waves"
    ]


def test_first_complete_clean_scope_retires_without_a_second_review(candidate, monkeypatch):
    target = unreviewed(candidate)
    calls = launch(monkeypatch, [{}])
    receipt = local_review.review(candidate)
    assert target.exists()
    assert [r["iteration"]["number"] for r in calls] == [1]
    instructions = calls[0]["iteration"]["instructions"]
    assert "read EVERY assigned diff page" in instructions
    assert "relevant current task obligations remain required" in instructions
    assert "A completed zero-new scope retires" in instructions
    assert all(call["iteration"]["excluded_finding_ids"] == [] for call in calls)
    assert all(call["iteration"]["recorded_findings"] == [] for call in calls)
    rounds = receipt_waves(candidate, receipt)
    assert len(rounds) == 1
    assert local_review.verified(candidate, receipt["payload"]["head"]) == receipt
    calls = launch(monkeypatch, [])
    assert local_review.review(candidate) == receipt
    assert calls == []


def test_new_defect_during_incomplete_scope_retry_never_leaves_approval_publishable(
    candidate, monkeypatch
):
    target = unreviewed(candidate)
    launch(monkeypatch, [{"coverage": False, "approved": False}])
    with pytest.raises(RuntimeError, match="complete diff coverage missing"):
        local_review.review(candidate)
    assert not target.exists()
    calls = launch(
        monkeypatch,
        [
            {"findings": ["[P1] New source defect"], "approved": False},
            {"approved": False, "alignment": "not_aligned"},
        ],
    )
    with pytest.raises(review_iterations.DiscoveryBatchError) as error:
        local_review.review(candidate)
    assert len(calls) == 2
    assert error.value.batch["status"] == "needs_fixes"
    assert not target.exists()
    with pytest.raises(RuntimeError, match="Fresh independent"):
        local_review.verified(candidate, git(candidate, "rev-parse", "HEAD"))


def test_fixed_head_requires_fresh_scope_without_historical_finding_text(candidate, monkeypatch):
    target = unreviewed(candidate)
    finding = "[P1] Original defect"
    launch(
        monkeypatch,
        [
            {"findings": [finding], "approved": False},
            {"approved": False, "alignment": "not_aligned"},
        ],
    )
    with pytest.raises(review_iterations.DiscoveryBatchError):
        local_review.review(candidate)
    assert not target.exists()
    head = commit(candidate, {"reviewed.py": "VALUE = 2\n"})
    sentinel = "AUTHOR_NAV_OLD_FIXED_ISSUE_DO_NOT_FORWARD"
    monkeypatch.setattr(
        local_review,
        "review_evidence",
        lambda root, subject: {
            "historical_fix_summary": finding,
            "author_navigation": {"old_fix": finding, "sentinel": sentinel},
        },
    )
    calls = launch(monkeypatch, [{}])
    receipt = local_review.review(candidate)
    assert [call["iteration"]["number"] for call in calls] == [1]
    for call in calls:
        assert call["iteration"]["excluded_finding_ids"] == []
        assert call["iteration"]["prior_findings"] == []
        assert call["iteration"]["recorded_findings"] == []
        assert finding not in json.dumps(call)
        assert sentinel not in json.dumps(call)
        assert call["user_task"] == task_context.protocol().public_context(
            task_context.read(candidate)
        )
    assert receipt["payload"]["head"] == head
    assert receipt["payload"]["review_iterations"]["completed_rounds"] == 1
    assert all(round_["payload"]["head"] == head for round_ in receipt_waves(candidate, receipt))
    retained = [
        attempt["result"]["payload"]["verdict"]["findings"]
        for attempt in review_iterations.history(candidate)["attempts"]
        if "result" in attempt
    ]
    assert [finding] in retained


@pytest.mark.parametrize("priority", ["P1", "P2"])
def test_duplicate_unresolved_issue_is_not_a_clean_terminal_round(candidate, monkeypatch, priority):
    previous = review_iterations.history(candidate)["attempts"]
    target = unreviewed(candidate)
    finding = f"[{priority}] Still unresolved"
    calls = launch(
        monkeypatch,
        [
            {"findings": [finding], "approved": False},
            {"findings": [finding], "approved": False},
        ],
    )
    with pytest.raises(RuntimeError, match="repeated recorded finding IDs are not novel") as error:
        local_review.review(candidate)
    assert "incomplete" in str(error.value).lower()
    assert len(calls) == 2
    assert calls[1]["iteration"]["excluded_finding_ids"] == [finding_id(finding)]
    retained = [
        result["payload"]["verdict"]["findings"] for result in current_results(candidate, previous)
    ]
    assert retained == [[finding], [finding]]
    assert not target.exists()
    calls = launch(monkeypatch, [{"approved": False, "alignment": "not_aligned"}])
    with pytest.raises(review_iterations.DiscoveryBatchError) as error:
        local_review.review(candidate)
    assert len(calls) == 1
    assert calls[0]["iteration"]["excluded_finding_ids"] == [finding_id(finding)]
    assert error.value.batch["status"] == "needs_fixes"
    assert not target.exists()


def test_identical_finding_on_changed_source_is_new_discovery(candidate, monkeypatch):
    unreviewed(candidate)
    old_head = git(candidate, "rev-parse", "HEAD")
    finding = "[P1] Native assignment remains invalid on the current source"
    outcomes = [
        {"findings": [finding], "approved": False},
        {"approved": False, "alignment": "not_aligned"},
    ]
    launch(monkeypatch, outcomes.copy())
    assert local_review.discover(candidate)["status"] == "needs_fixes"
    head = commit(candidate, {"reviewed.py": "VALUE = 2\n"})
    calls = launch(monkeypatch, outcomes.copy())
    batch = local_review.discover(candidate)
    assert batch["status"] == "needs_fixes"
    assert len(calls) == 2
    for key in ("prior_findings", "recorded_findings", "excluded_finding_ids"):
        assert calls[0]["iteration"][key] == []
    assert calls[1]["iteration"]["excluded_finding_ids"] == [finding_id(finding)]
    assert [item["id"] for item in batch["findings"]] == [finding_id(finding)]
    origins = [
        attempt["result"]["payload"]["head"]
        for attempt in review_iterations.history(candidate)["attempts"]
        if attempt.get("result", {}).get("payload", {}).get("verdict", {}).get("findings")
        == [finding]
    ]
    assert origins == [old_head, head]
    assert not (local_review.directory(candidate) / (head + ".json")).exists()


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), RuntimeError("interrupted")])
def test_interrupted_scope_resumes_without_publishing_an_incomplete_result(
    candidate, monkeypatch, failure
):
    target = unreviewed(candidate)
    interrupted_calls = launch(monkeypatch, [failure])
    with pytest.raises(type(failure)):
        local_review.review(candidate)
    assert not target.exists()
    calls = launch(monkeypatch, [{}])
    receipt = local_review.review(candidate)
    assert [r["iteration"]["number"] for r in interrupted_calls] == [1]
    assert [r["iteration"]["number"] for r in calls] == [2]
    assert calls[0]["review_chunk"]["chunk"] == interrupted_calls[0]["review_chunk"]["chunk"]
    assert calls[0]["review_chunk"]["wave_id"] != interrupted_calls[0]["review_chunk"]["wave_id"]
    assert calls[0]["iteration"]["recorded_findings"] == []
    assert calls[0]["iteration"]["excluded_finding_ids"] == []
    assert local_review.verified(candidate, receipt["payload"]["head"]) == receipt
    assert local_review.review(candidate) == receipt
    assert len(calls) == 1


def test_incomplete_verdict_requires_one_fresh_complete_scope(candidate, monkeypatch):
    target = unreviewed(candidate)
    launch(monkeypatch, [{"approved": False, "coverage": False}])
    with pytest.raises(RuntimeError, match="complete diff coverage missing"):
        local_review.review(candidate)
    assert not target.exists()
    calls = launch(monkeypatch, [{}])
    local_review.review(candidate)
    assert len(calls) == 1


def test_request_requires_bounded_complete_inspection_but_still_blocks_incomplete_round(
    candidate, monkeypatch
):
    previous = review_iterations.history(candidate)["attempts"]
    target = unreviewed(candidate)
    calls = launch(
        monkeypatch,
        [{"approved": False, "coverage": False, "summary": "Only five paths inspected"}],
    )
    with pytest.raises(RuntimeError, match="complete diff coverage missing") as error:
        local_review.review(candidate)
    assert "Only five paths inspected" in str(error.value)
    assert len(calls) == 1
    request = calls[0]
    instructions = request["iteration"]["instructions"]
    assert "complete assigned immutable scope in bounded groups" in instructions
    assert "semantically assessing" in instructions
    assert "read EVERY assigned diff page" in instructions
    assert "coordinator aggregates ALL chunks" in instructions
    assert "small initial sample or a size estimate while work can continue" in instructions
    assert "without stopping for repair" in instructions
    assert "Concrete findings may still stop a round for repair" not in instructions
    assert "verify every previously reported issue" not in instructions.lower()
    assert "tool failures, unresolved uncertainty" in instructions
    assert "still require rejection; never pressure approval or relax coverage" in instructions
    assert request["iteration"]["minimum_completed_rounds"] == 1
    assert request["user_task"] == task_context.protocol().public_context(
        task_context.read(candidate)
    )
    assert not target.exists()
    outcomes = current_results(candidate, previous)
    assert [review_iterations.classification(r["payload"]) for r in outcomes] == ["incomplete"]


def test_concrete_issue_is_actionable_even_when_summary_describes_incomplete_scope(
    candidate, monkeypatch
):
    previous = review_iterations.history(candidate)["attempts"]
    target = unreviewed(candidate)
    finding = "[P1] type_resolution.py::validate_profile accepts out-of-range date-only timestamps"
    launch(
        monkeypatch,
        [
            {
                "approved": False,
                "findings": [finding],
                "summary": "Only 10 paths assessed; incomplete",
                "coverage": False,
            },
        ],
    )
    with pytest.raises(RuntimeError, match="complete diff coverage missing") as error:
        local_review.review(candidate)
    assert finding in str(error.value)
    assert "Only 10 paths assessed; incomplete" in str(error.value)
    assert not target.exists()
    calls = launch(
        monkeypatch,
        [{"approved": False, "alignment": "not_aligned"}],
    )
    with pytest.raises(review_iterations.DiscoveryBatchError) as error:
        local_review.review(candidate)
    assert finding in str(error.value)
    assert len(calls) == 1
    assert calls[0]["iteration"]["excluded_finding_ids"] == [finding_id(finding)]
    assert not target.exists()
    completed = current_results(candidate, previous)
    assert len(completed) == 2
    assert completed[0]["payload"]["verdict"]["findings"] == [finding]
    assert completed[0]["payload"]["coverage_complete"] is False
    assert completed[1]["payload"]["coverage_complete"] is True


def test_approved_verdict_with_unresolved_findings_is_never_clean(candidate, monkeypatch):
    previous = review_iterations.history(candidate)["attempts"]
    target = unreviewed(candidate)
    finding = "[P2] Unresolved despite approval"
    launch(
        monkeypatch,
        [{"findings": [finding], "approved": True}],
    )
    with pytest.raises(RuntimeError, match="rejected verdict"):
        local_review.review(candidate)
    assert not target.exists()
    completed = current_results(candidate, previous)
    assert len(completed) == 1
    first = completed[0]["payload"]
    assert first["coverage_complete"] is False
    assert first["verdict"]["findings"] == [finding]
    assert first["review_wave"]["children"][0]["payload"]["verdict"]["approved"] is True
    launch(
        monkeypatch,
        [{"approved": False, "alignment": "not_aligned"}],
    )
    with pytest.raises(review_iterations.DiscoveryBatchError):
        local_review.review(candidate)
    assert not target.exists()


def test_reused_reviewer_identity_cannot_supply_an_issue_scope_retry(candidate, monkeypatch):
    target = unreviewed(candidate)
    identity = "reviewer:" + str(uuid.uuid4())
    calls = launch(
        monkeypatch,
        [
            {"reviewer_id": identity, "findings": ["[P2] Scope needs repair"], "approved": False},
            {"reviewer_id": identity, "approved": False, "alignment": "not_aligned"},
        ],
    )
    with pytest.raises(RuntimeError, match="fresh reviewer identity"):
        local_review.review(candidate)
    assert len(calls) == 2
    assert not target.exists()


@pytest.mark.parametrize("change", ["task", "base"])
def test_changed_task_or_base_invalidates_round_cache(candidate, monkeypatch, change):
    unreviewed(candidate)
    intermediate = git(candidate, "rev-parse", "HEAD")
    commit(candidate, {"reviewed.py": "VALUE = 2\n"})
    finding = "[P2] Source context before its exact binding changes"
    launch(monkeypatch, [{"findings": [finding], "approved": False}, RuntimeError("pause")])
    with pytest.raises(RuntimeError, match="pause"):
        local_review.review(candidate)
    if change == "task":
        task_context.record(candidate, "Additional task requirement", append=True)
        commit(candidate, {"reviewed.py": "VALUE = 3\n"})
    else:
        state = sessions.load(candidate)
        state["base"] = intermediate
        sessions.save(candidate, state)
    subject = local_review.binding(candidate, git(candidate, "rev-parse", "HEAD"))
    plan = review_chunks.propose(candidate, subject, local_review_files)
    review_chunks.install(candidate, subject, local_review_files, plan)
    calls = launch(monkeypatch, [{}])
    local_review.review(candidate)
    assert [r["iteration"]["number"] for r in calls] == [1]
    assert calls[0]["iteration"]["excluded_finding_ids"] == []
    assert finding not in json.dumps(calls[0])


def test_signed_round_history_survives_restart_and_detects_tampering(candidate, monkeypatch):
    unreviewed(candidate)
    launch(
        monkeypatch,
        [
            {"findings": ["[P2] Signed historical observation"], "approved": False},
            RuntimeError("pause"),
        ],
    )
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
    calls = launch(monkeypatch, [{}])
    lock_path = local_review.directory(candidate) / "iterations.lock"
    with bootstrap_process.lock(lock_path, wait=False), pytest.raises(
        RuntimeError, match="already running"
    ):
        local_review.review(candidate)
    assert calls == []
    local_review.review(candidate)
    assert [x["iteration"]["number"] for x in calls] == [1]


def test_completed_terminal_round_survives_interruption_before_final_receipt(
    candidate, monkeypatch
):
    target = unreviewed(candidate)
    count = 0
    original_record = review_iterations.record

    def record(root, attempt_id, receipt):
        nonlocal count
        original_record(root, attempt_id, receipt)
        if receipt["payload"].get("review_scope") != "wave":
            return
        count += 1
        if count == 1:
            raise KeyboardInterrupt

    launch(monkeypatch, [{}])
    monkeypatch.setattr(review_iterations, "record", record)
    with pytest.raises(KeyboardInterrupt):
        local_review.review(candidate)
    assert not target.exists()
    calls = launch(monkeypatch, [])
    receipt = local_review.review(candidate)
    assert calls == []
    assert receipt["payload"]["review_iterations"]["completed_rounds"] == 1


def test_discovery_collects_fresh_issues_until_complete_zero_without_repair(candidate, monkeypatch):
    previous = review_iterations.history(candidate)["attempts"]
    target = unreviewed(candidate)
    original_head = git(candidate, "rev-parse", "HEAD")
    first = "[P1] First independent defect"
    second = "[P2] Previously undiscovered defect"
    calls = launch(
        monkeypatch,
        [
            {"findings": [first], "approved": False},
            {"findings": [second], "approved": False},
            {"approved": False, "alignment": "not_aligned"},
        ],
    )
    batch = local_review.discover(candidate)
    assert batch["status"] == "needs_fixes"
    assert batch["approval_eligible"] is False
    assert len(batch["rounds"]) == 1
    assert len(current_results(candidate, previous)) == 3
    assert [call["iteration"]["number"] for call in calls] == [1, 2, 3]
    assert all(call["iteration"]["discovery_only"] for call in calls)
    assert calls[0]["iteration"]["excluded_finding_ids"] == []
    assert calls[1]["iteration"]["excluded_finding_ids"] == [finding_id(first)]
    assert first in json.dumps(calls[1])
    assert second not in json.dumps(calls[1])
    assert set(calls[2]["iteration"]["excluded_finding_ids"]) == {
        finding_id(first),
        finding_id(second),
    }
    assert git(candidate, "rev-parse", "HEAD") == original_head
    assert not target.exists()
    calls = launch(monkeypatch, [])
    assert local_review.discover(candidate)["status"] == "needs_fixes"
    assert calls == []


def test_v3_scope_is_audit_only_and_cannot_supply_current_v4_approval(candidate, monkeypatch):
    target = unreviewed(candidate)
    subject = local_review.binding(candidate, git(candidate, "rev-parse", "HEAD"))
    plan = review_chunks.propose(candidate, subject, local_review_files)
    legacy = signed_round(
        candidate,
        subject,
        modern=True,
        review_chunk={
            "plan_sha256": review_chunks.plan_digest(plan),
            "wave_id": uuid.uuid4().hex,
            "chunk": plan["chunks"][0],
        },
        procedure="discovery-chunks-v3",
    )
    review_iterations._save(
        candidate,
        {
            "version": 1,
            "attempts": [
                {
                    "id": "legacy-completed-round",
                    "subject": subject,
                    "iteration": {"number": 1},
                    "result": legacy,
                }
            ],
        },
    )
    calls = launch(monkeypatch, [{}])
    receipt = local_review.review(candidate)
    assert [call["iteration"]["number"] for call in calls] == [1]
    assert target.exists()
    assert all(
        round_["payload"].get("review_procedure") == "discovery-chunks-v4"
        for round_ in receipt_waves(candidate, receipt)
    )
    assert review_iterations.history(candidate)["attempts"][0]["result"] == legacy


@pytest.mark.parametrize("incomplete", [{"coverage": False}, {"alignment": "uncertain"}])
def test_incomplete_zero_new_round_cannot_close_discovery(candidate, monkeypatch, incomplete):
    target = unreviewed(candidate)
    finding = "[P1] Current immutable source defect"
    launch(
        monkeypatch,
        [
            {"findings": [finding], "approved": False},
            {"approved": False, "alignment": "not_aligned", **incomplete},
        ],
    )
    with pytest.raises(RuntimeError, match="incomplete"):
        local_review.discover(candidate)
    assert not target.exists()
    calls = launch(monkeypatch, [{"approved": False, "alignment": "not_aligned"}])
    assert local_review.discover(candidate)["status"] == "needs_fixes"
    assert calls[0]["iteration"]["excluded_finding_ids"] == [finding_id(finding)]


def test_partial_round_keeps_concrete_findings_but_cannot_claim_zero_new_convergence(
    candidate, monkeypatch
):
    target = unreviewed(candidate)
    finding = "[P2] Concrete finding before a failed source read"
    calls = launch(
        monkeypatch,
        [{"findings": [finding], "coverage": False, "approved": False}],
    )
    with pytest.raises(RuntimeError, match="complete diff coverage missing"):
        local_review.discover(candidate)
    assert len(calls) == 1
    original_wave = calls[0]["review_chunk"]["wave_id"]
    assert not target.exists()
    calls = launch(monkeypatch, [{"approved": False, "alignment": "not_aligned"}])
    batch = local_review.discover(candidate)
    assert batch["status"] == "needs_fixes"
    assert len(calls) == 1
    assert calls[0]["review_chunk"]["wave_id"] != original_wave
    assert calls[0]["iteration"]["excluded_finding_ids"] == [finding_id(finding)]
    assert batch["finding_ids"] == [finding_id(finding)]
    assert not target.exists()


def test_discovery_mode_never_runs_checks_or_certifies_uncommitted_procedure_files(
    candidate, monkeypatch
):
    target = unreviewed(candidate)
    committed_head = git(candidate, "rev-parse", "HEAD")
    committed_tree = git(candidate, "rev-parse", "HEAD^{tree}")
    tracked = candidate / "reviewed.py"
    untracked = candidate / "procedure_only.py"
    tracked.write_text("UNCOMMITTED_PROCEDURE = True\n")
    untracked.write_text("UNTRACKED_PROCEDURE = True\n")
    dirty_state = git(candidate, "status", "--porcelain")

    def forbidden_checks(root):
        pytest.fail("Discovery-only must not execute validation checks")

    def forbidden_evidence(root, subject):
        pytest.fail("Discovery-only must not read historical author navigation")

    monkeypatch.setattr(local_review, "validation", forbidden_checks)
    monkeypatch.setattr(local_review, "review_evidence", forbidden_evidence)
    calls = launch(monkeypatch, [{}])
    batch = local_review.discover(candidate)
    assert batch["status"] == "discovery_complete"
    assert batch["approval_eligible"] is False
    assert batch["rounds"][-1]["payload"]["verdict"]["approved"] is False
    assert all(call["subject"]["head"] == committed_head for call in calls)
    assert all(call["subject"]["tree"] == committed_tree for call in calls)
    assert all(call["evidence"] == {} for call in calls)
    assert all(
        call["user_task"] == task_context.protocol().public_context(task_context.read(candidate))
        for call in calls
    )
    assert git(candidate, "status", "--porcelain") == dirty_state
    assert tracked.read_text() == "UNCOMMITTED_PROCEDURE = True\n"
    assert untracked.read_text() == "UNTRACKED_PROCEDURE = True\n"
    assert not target.exists()


def test_discovery_rounds_cannot_substitute_for_final_candidate_approval_rounds(
    candidate, monkeypatch
):
    target = unreviewed(candidate)
    launch(monkeypatch, [{}])
    batch = local_review.discover(candidate)
    assert batch["status"] == "discovery_complete"
    assert not target.exists()
    calls = launch(monkeypatch, [{}])
    receipt = local_review.review(candidate)
    assert [call["iteration"]["number"] for call in calls] == [1]
    assert all(not call["iteration"]["discovery_only"] for call in calls)
    assert all(
        not round_["payload"]["discovery_only"] for round_ in receipt_waves(candidate, receipt)
    )


def test_diagnostic_task_cannot_launder_known_issues_on_the_same_source(candidate, monkeypatch):
    target = unreviewed(candidate)
    finding = "[P1] Existing source issue remains unresolved"
    launch(
        monkeypatch,
        [
            {"findings": [finding], "approved": False},
            {"approved": False, "alignment": "not_aligned"},
        ],
    )
    assert local_review.discover(candidate)["status"] == "needs_fixes"
    subject = local_review.binding(candidate, git(candidate, "rev-parse", "HEAD"))
    subject["task_digest"] = "f" * 64
    plan = review_chunks.propose(candidate, subject, local_review_files)
    calls = []

    def execute(request):
        parsed = json.loads(request)
        iteration = parsed["iteration"]
        calls.append(iteration)
        return signed_round(
            candidate,
            subject,
            modern=True,
            review_chunk=parsed["review_chunk"],
            discovery_only=False,
            recorded_findings=iteration["recorded_findings"],
            prior_findings=iteration["prior_findings"],
            previous_rounds_sha256=iteration["previous_rounds_sha256"],
            approved=False,
            alignment="not_aligned",
        )

    def sign_wave(payload):
        job = local_review.directory(candidate) / ("fixture-aggregate-" + uuid.uuid4().hex)
        job.mkdir()
        return local_review._sign_payload(candidate, job, payload)

    def verify_wave(manifest, ledgers):
        review_chunks.aggregate_coverage(local_review_files, candidate, subject, manifest, ledgers)

    with pytest.raises(review_iterations.DiscoveryBatchError) as error:
        review_iterations.run(
            candidate,
            subject,
            {"subject": subject, "review_plan": plan},
            execute,
            sign_wave=sign_wave,
            verify_wave=verify_wave,
        )
    assert error.value.batch["status"] == "needs_fixes"
    assert {item["id"] for item in error.value.batch["findings"]} == {finding_id(finding)}
    assert all(finding_id(finding) in call["excluded_finding_ids"] for call in calls)
    assert not target.exists()
