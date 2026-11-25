"""Retire only latest complete zero-new component and integration observations."""

from __future__ import annotations

import copy
import json
import threading
import uuid
from collections import Counter

import pytest

from agent_tools import (
    local_review,
    local_review_files,
    review_chunks,
    review_clearances,
    review_iterations,
    review_waves,
)
from tests.agent_tools._support.local_review import signed_round
from tests.agent_tools.local_review_evidence import (
    candidate as candidate,  # noqa: PLC0414 - shared fixture or compatibility export.
)
from tests.agent_tools.local_review_evidence import commit
from tests.agent_tools.sessions import git
from tests.agent_tools.sessions import (
    source as source,  # noqa: PLC0414 - shared fixture or compatibility export.
)


def install_plan(root):
    subject = local_review.binding(root, git(root, "rev-parse", "HEAD"))
    plan = review_chunks.propose(root, subject, local_review_files, max_workers=2, max_paths=2)
    if 2 <= len(plan["inventory"]) <= 3:
        paths = [item["path"] for item in plan["inventory"]]
        plan["limits"]["max_paths"] = len(paths)
        groups = [paths[:-1], paths[-1:]]
        plan["components"] = [
            {"id": f"component-{index}", "paths": group} for index, group in enumerate(groups)
        ]
        plan["chunks"] = [
            {
                "id": f"scope-{index}",
                "kind": "component",
                "component_ids": [f"component-{index}"],
                "paths": group,
                "purpose": "Inspect this component and its current task obligations.",
            }
            for index, group in enumerate(groups)
        ]
        plan["chunks"].append(
            {
                "id": "scope-integration",
                "kind": "integration",
                "component_ids": [component["id"] for component in plan["components"]],
                "paths": paths,
                "purpose": "Inspect the contract between both components.",
            }
        )
    return review_chunks.install(root, subject, local_review_files, plan)


@pytest.fixture
def assigned_candidate(candidate):
    commit(
        candidate,
        {
            "alpha/source.py": "ALPHA = 1\n",
            "beta/source.py": "BETA = 2\n",
        },
    )
    plan = install_plan(candidate)
    assert len(plan["chunks"]) > plan["limits"]["max_workers"]
    assert any(chunk["kind"] == "integration" for chunk in plan["chunks"])
    return candidate, plan


def child_receipt(root, subject, request, **outcome):
    parsed = json.loads(request)
    iteration = parsed["iteration"]
    return signed_round(
        root,
        subject,
        modern=True,
        review_chunk=parsed["review_chunk"],
        discovery_only=iteration["discovery_only"],
        recorded_findings=iteration["recorded_findings"],
        prior_findings=iteration["prior_findings"],
        previous_rounds_sha256=iteration["previous_rounds_sha256"],
        **outcome,
    )


def observed_wave(root, manifest, outcomes, *, discovery_only=False, procedure=None):
    iteration = {
        "attempt_id": uuid.uuid4().hex,
        "number": 1,
        "minimum_completed_rounds": 1,
        "wave_id": uuid.uuid4().hex,
        "discovery_only": discovery_only,
        "procedure": procedure or "discovery-chunks-v4",
        "recorded_findings": [],
        "prior_findings": [],
        "excluded_finding_ids": [],
        "previous_rounds_sha256": review_iterations.findings_digest([]),
    }
    wave = {
        "id": iteration["wave_id"],
        "subject": manifest["subject"],
        "manifest": manifest,
        "plan_sha256": review_chunks.plan_digest(manifest),
        "discovery_only": discovery_only,
        "iteration": iteration,
        "outcomes": [],
        "complete": False,
    }
    for chunk in manifest["chunks"]:
        if chunk["id"] not in outcomes:
            continue
        child = signed_round(
            root,
            manifest["subject"],
            modern=True,
            discovery_only=discovery_only,
            review_chunk={
                "plan_sha256": wave["plan_sha256"],
                "wave_id": wave["id"],
                "chunk": chunk,
            },
            procedure=procedure,
            **outcomes[chunk["id"]],
        )
        review_waves.verify_chunk(root, child, manifest["subject"], manifest, wave["id"])
        if child["payload"]["coverage_complete"]:
            review_chunks.scoped_coverage(
                local_review_files,
                root,
                manifest["subject"],
                manifest,
                chunk["id"],
                local_review.Path(child["payload"]["coverage_ledger"]),
            )
        wave["outcomes"].append(
            {"chunk_id": chunk["id"], "attempt_id": uuid.uuid4().hex, "result": child}
        )
    return wave


def test_parallel_waves_use_bounded_workers_and_complete_every_assignment(
    assigned_candidate, monkeypatch
):
    root, plan = assigned_candidate
    rendezvous = threading.Barrier(2)
    lock = threading.Lock()
    requests, children = [], []
    active = peak = 0

    def execute(root, job, subject, request):
        nonlocal active, peak
        parsed = json.loads(request)
        with lock:
            requests.append(parsed)
            active += 1
            peak = max(peak, active)
            first_pair = len(requests) <= 2
        try:
            if first_pair:
                rendezvous.wait(timeout=10)
            assert not (local_review.directory(root) / (subject["head"] + ".json")).exists()
            receipt = child_receipt(root, subject, request)
            with lock:
                children.append(receipt)
            return receipt
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(local_review, "_execute_review", execute)
    receipt = local_review.review(root)
    assert peak == plan["limits"]["max_workers"] == 2
    assert active == 0
    assert len(requests) == len(plan["chunks"])
    wave_ids = {request["review_chunk"]["wave_id"] for request in requests}
    assert len(wave_ids) == 1
    expected = {chunk["id"] for chunk in plan["chunks"]}
    for wave_id in wave_ids:
        assert {
            request["review_chunk"]["chunk"]["id"]
            for request in requests
            if request["review_chunk"]["wave_id"] == wave_id
        } == expected
    assert len({child["payload"]["reviewer_id"] for child in children}) == len(children)
    assert local_review.verified(root, receipt["payload"]["head"]) == receipt


@pytest.mark.parametrize("issue_kind", ["component", "integration"])
def test_clean_scope_retires_while_issue_and_incomplete_scopes_retry(
    assigned_candidate, monkeypatch, issue_kind
):
    root, plan = assigned_candidate
    issue_id = next(chunk["id"] for chunk in plan["chunks"] if chunk["kind"] == issue_kind)
    failed_id = next(chunk["id"] for chunk in plan["chunks"] if chunk["kind"] != issue_kind)
    clean_id = next(
        chunk["id"] for chunk in plan["chunks"] if chunk["id"] not in {issue_id, failed_id}
    )
    finding = "[P2] Current cross-component contract is inconsistent"
    first_requests = []

    def partial(root, job, subject, request):
        parsed = json.loads(request)
        first_requests.append(parsed)
        chunk_id = parsed["review_chunk"]["chunk"]["id"]
        if chunk_id == failed_id:
            return child_receipt(root, subject, request, coverage=False, approved=False)
        outcome = {"findings": [finding], "approved": False} if chunk_id == issue_id else {}
        return child_receipt(root, subject, request, **outcome)

    monkeypatch.setattr(local_review, "_execute_review", partial)
    with pytest.raises(RuntimeError, match=r"(?i)incomplete|failed|worker"):
        local_review.discover(root)
    assert len(first_requests) == len(plan["chunks"])
    wave_id = first_requests[0]["review_chunk"]["wave_id"]
    resumed = []

    def complete(root, job, subject, request):
        parsed = json.loads(request)
        resumed.append(parsed)
        known = parsed["iteration"]["recorded_findings"]
        outcome = {"approved": False, "alignment": "not_aligned"} if known else {}
        return child_receipt(root, subject, request, **outcome)

    monkeypatch.setattr(local_review, "_execute_review", complete)
    batch = local_review.discover(root)
    assert batch["status"] == "needs_fixes"
    assert len(resumed) == 2
    counts = Counter(
        request["review_chunk"]["chunk"]["id"] for request in [*first_requests, *resumed]
    )
    assert counts == {clean_id: 1, issue_id: 2, failed_id: 2}
    assert all(request["review_chunk"]["wave_id"] != wave_id for request in resumed)
    assert all(finding in json.dumps(request) for request in resumed)
    final = batch["rounds"][-1]["payload"]["review_wave"]
    assert {child["payload"]["chunk_id"] for child in final["children"]} == set(counts)
    origins = {
        child["payload"]["chunk_id"]: origin["wave_id"]
        for child, origin in zip(final["children"], final["origins"])
    }
    assert origins[clean_id] == wave_id
    assert origins[issue_id] != wave_id
    assert origins[failed_id] != wave_id
    assert not (local_review.directory(root) / (plan["subject"]["head"] + ".json")).exists()


@pytest.mark.parametrize("change", ["plan", "head"])
def test_changed_candidate_or_plan_never_reuses_completed_partial_wave_workers(
    assigned_candidate, monkeypatch, change
):
    root, plan = assigned_candidate
    failed_id = plan["chunks"][0]["id"]
    initial = []

    def interrupted(root, job, subject, request):
        parsed = json.loads(request)
        initial.append(parsed)
        if parsed["review_chunk"]["chunk"]["id"] == failed_id:
            msg = "incomplete worker"
            raise RuntimeError(msg)
        return child_receipt(root, subject, request)

    monkeypatch.setattr(local_review, "_execute_review", interrupted)
    with pytest.raises(RuntimeError, match=r"incomplete|failed|worker"):
        local_review.discover(root)
    if change == "plan":
        replacement = copy.deepcopy(plan)
        replacement["author_plan"] += " Also inspect the explicitly named integration obligations."
        plan = review_chunks.install(root, plan["subject"], local_review_files, replacement)
    else:
        commit(root, {"delta/source.py": "DELTA = 4\n"})
        plan = install_plan(root)
    current = []

    def execute(root, job, subject, request):
        parsed = json.loads(request)
        current.append(parsed)
        return child_receipt(root, subject, request)

    monkeypatch.setattr(local_review, "_execute_review", execute)
    batch = local_review.discover(root)
    assert batch["status"] == "discovery_complete"
    assert len(current) == len(plan["chunks"])
    assert {request["review_chunk"]["wave_id"] for request in current}.isdisjoint(
        request["review_chunk"]["wave_id"] for request in initial
    )
    assert all(request["iteration"]["excluded_finding_ids"] == [] for request in current)


def test_issue_scope_retry_requires_a_fresh_reviewer_without_rerunning_clean_scopes(
    assigned_candidate, monkeypatch
):
    root, plan = assigned_candidate
    issue_id = plan["chunks"][0]["id"]
    identity = "reviewer:" + str(uuid.uuid4())
    requests = []

    def execute(root, job, subject, request):
        parsed = json.loads(request)
        requests.append(parsed)
        outcome = {}
        if parsed["review_chunk"]["chunk"]["id"] == issue_id:
            outcome = {"reviewer_id": identity, "approved": False, "alignment": "not_aligned"}
            if not parsed["iteration"]["recorded_findings"]:
                outcome["findings"] = ["[P2] Current scope issue"]
        return child_receipt(root, subject, request, **outcome)

    monkeypatch.setattr(local_review, "_execute_review", execute)
    with pytest.raises(RuntimeError, match="fresh reviewer identity"):
        local_review.review(root)
    counts = Counter(request["review_chunk"]["chunk"]["id"] for request in requests)
    assert counts[issue_id] == 2
    assert all(count == 1 for chunk_id, count in counts.items() if chunk_id != issue_id)
    assert not (local_review.directory(root) / (plan["subject"]["head"] + ".json")).exists()


@pytest.mark.parametrize("later", ["positive", "incomplete"])
@pytest.mark.parametrize("scope_kind", ["component", "integration"])
def test_legacy_adoption_uses_latest_observation_even_inside_a_partial_wave(
    assigned_candidate, later, scope_kind
):
    root, original = assigned_candidate
    current = copy.deepcopy(original)
    current["subject"]["task_digest"] = "f" * 64
    current["reuse_discovery_from"] = {
        "task_digest": original["subject"]["task_digest"],
        "plan_sha256": review_chunks.plan_digest(original),
        "discovery_only": True,
    }
    review_chunks.validate_discovery_reuse(current, original)
    first = observed_wave(
        root,
        original,
        {chunk["id"]: {} for chunk in original["chunks"]},
        discovery_only=True,
        procedure="discovery-chunks-v3",
    )
    changed_id = next(chunk["id"] for chunk in original["chunks"] if chunk["kind"] == scope_kind)
    outcome = {"approved": False, "coverage": False}
    if later == "positive":
        outcome = {"approved": False, "findings": ["[P2] Later original-scope observation"]}
    second = observed_wave(
        root, original, {changed_id: outcome}, discovery_only=True, procedure="discovery-chunks-v3"
    )
    state = {"waves": [first, second]}
    selected = review_clearances.latest(
        state, current, discovery_only=True, complete=review_waves._complete_chunk
    )
    assert changed_id not in selected
    assert set(selected) == {chunk["id"] for chunk in original["chunks"]} - {changed_id}
    assert first["complete"] is False
    for child, origin in selected.values():
        assert origin["wave_id"] == first["id"]
        assert child["payload"]["review_procedure"] == "discovery-chunks-v3"
        review_clearances.verify_origin(current, origin, child["payload"], discovery_only=True)
    assert (
        review_clearances.latest(
            state, current, discovery_only=False, complete=review_waves._complete_chunk
        )
        == {}
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "chunk",
        "duplicate_chunk",
        "plan",
        "scope",
        "signature",
        "coverage",
        "identity",
    ],
)
def test_invalid_worker_provenance_or_coverage_never_supplies_aggregate_approval(
    assigned_candidate, monkeypatch, fault
):
    root, plan = assigned_candidate
    first_id = plan["chunks"][0]["id"]
    shared_identity = "reviewer:" + str(uuid.uuid4())
    observed = []

    def execute(root, job, subject, request):
        parsed = json.loads(request)
        observed.append(parsed)
        chosen = parsed["review_chunk"]["chunk"]["id"] == first_id
        if chosen and fault == "missing":
            return None
        if chosen and fault in {"chunk", "duplicate_chunk", "plan"}:
            parsed = copy.deepcopy(parsed)
            if fault in {"chunk", "duplicate_chunk"}:
                parsed["review_chunk"]["chunk"]["id"] = (
                    "unassigned-chunk" if fault == "chunk" else plan["chunks"][1]["id"]
                )
            else:
                parsed["review_chunk"]["plan_sha256"] = "f" * 64
            request = json.dumps(parsed)
        outcome = {"coverage": False} if chosen and fault == "coverage" else {}
        if fault == "identity":
            outcome["reviewer_id"] = shared_identity
        receipt = child_receipt(root, subject, request, **outcome)
        if chosen and fault == "signature":
            receipt["payload"]["verdict"]["summary"] = "Unsigned replacement summary"
        if chosen and fault == "scope":
            receipt["payload"]["scope_sha256"] = "f" * 64
            receipt = local_review._sign_payload(root, job, receipt["payload"])
        return receipt

    monkeypatch.setattr(local_review, "_execute_review", execute)
    with pytest.raises(
        (RuntimeError, ValueError), match=r"incomplete|scope|signature|review|coverage"
    ):
        local_review.review(root)
    assert len(observed) == len(plan["chunks"])
    assert not (local_review.directory(root) / (plan["subject"]["head"] + ".json")).exists()


def test_large_complete_wave_proofs_remain_private_and_public_attestation_stays_bounded(
    candidate, monkeypatch
):
    commit(candidate, {f"component{index}/source.py": f"VALUE = {index}\n" for index in range(16)})
    plan = install_plan(candidate)

    def execute(root, job, subject, request):
        return child_receipt(root, subject, request)

    monkeypatch.setattr(local_review, "_execute_review", execute)
    receipt = local_review.review(candidate)
    compact = receipt["payload"]["review_iterations"]
    proof_path = local_review.directory(candidate) / ("proof-" + compact["proof_sha256"] + ".json")
    full_proof = proof_path.read_bytes()
    assert compact["version"] == 4
    assert compact["completed_waves"] == 1
    assert compact["chunk_count"] == len(plan["chunks"])
    assert len(full_proof) > 48 * 1024
    assert len(local_review.protocol(candidate).canonical(receipt)) < 48 * 1024
    assert "rounds" not in compact
    assert local_review.verified(candidate, receipt["payload"]["head"]) == receipt
    proof_path.unlink()
    with pytest.raises((RuntimeError, ValueError), match=r"proof|parallel"):
        local_review.verified(candidate, receipt["payload"]["head"])
    proof_path.write_bytes(full_proof + b" ")
    with pytest.raises((RuntimeError, ValueError), match=r"proof|parallel"):
        local_review.verified(candidate, receipt["payload"]["head"])


def test_later_incomplete_observation_invalidates_a_previously_clean_final_proof(
    assigned_candidate, monkeypatch
):
    root, plan = assigned_candidate

    def execute(root, job, subject, request):
        return child_receipt(root, subject, request)

    monkeypatch.setattr(local_review, "_execute_review", execute)
    receipt = local_review.review(root)
    assert local_review.verified(root, receipt["payload"]["head"]) == receipt
    wave = observed_wave(
        root, plan, {plan["chunks"][0]["id"]: {"coverage": False, "approved": False}}
    )
    state = review_waves._read(root)
    state["waves"].append(wave)
    review_waves._save(root, state)
    local_review.verify_signature(root, receipt)
    with pytest.raises((RuntimeError, ValueError), match=r"latest|clearance|incomplete"):
        local_review.verified(root, receipt["payload"]["head"])


def test_installing_a_new_plan_invalidates_approval_for_the_same_source_subject(
    assigned_candidate, monkeypatch
):
    root, plan = assigned_candidate
    requests = []

    def execute(root, job, subject, request):
        requests.append(json.loads(request))
        return child_receipt(root, subject, request)

    monkeypatch.setattr(local_review, "_execute_review", execute)
    receipt = local_review.review(root)
    assert local_review.verified(root, receipt["payload"]["head"]) == receipt
    target = local_review.directory(root) / (receipt["payload"]["head"] + ".json")
    original_bytes = target.read_bytes()
    assert len(requests) == len(plan["chunks"])
    replacement = copy.deepcopy(plan)
    replacement["chunks"][0]["purpose"] += " Explicitly inspect the component's exported contract."
    replacement = review_chunks.install(root, plan["subject"], local_review_files, replacement)
    assert replacement["subject"] == plan["subject"]
    assert review_chunks.plan_digest(replacement) != review_chunks.plan_digest(plan)
    with pytest.raises((RuntimeError, ValueError), match=r"[Pp]lan"):
        local_review.verified(root, receipt["payload"]["head"])
    fresh = local_review.review(root)
    assert fresh != receipt
    assert fresh["payload"]["review_iterations"]["plan_sha256"] == review_chunks.plan_digest(
        replacement
    )
    assert local_review.verified(root, fresh["payload"]["head"]) == fresh
    assert len(requests) == 2 * len(plan["chunks"])
    fresh_requests = requests[len(plan["chunks"]) :]
    assert Counter(request["review_chunk"]["chunk"]["id"] for request in fresh_requests) == {
        chunk["id"]: 1 for chunk in replacement["chunks"]
    }
    assert all(request["iteration"]["excluded_finding_ids"] == [] for request in fresh_requests)
    archives = list(
        local_review.directory(root).glob(receipt["payload"]["head"] + ".superseded-plan-*.json")
    )
    assert len(archives) == 1
    assert archives[0].read_bytes() == original_bytes
    local_review.verify_signature(root, json.loads(archives[0].read_text()))
    with pytest.raises((RuntimeError, ValueError), match=r"[Pp]lan"):
        review_iterations.validate_convergence(root, receipt["payload"])
    assert local_review.review(root) == fresh
    assert len(requests) == 2 * len(plan["chunks"])
