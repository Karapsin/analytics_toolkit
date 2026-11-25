"""Durable chunk-read evidence retains its scope without granting whole approval."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from agent_tools import local_review_files, review_checkpoints, review_chunks
from tests._support.paths import REPO_ROOT
from tests.agent_tools.sessions import git
from tests.agent_tools.sessions import (
    source as source,  # noqa: PLC0414 - shared fixture or compatibility export.
)

CheckpointCase = tuple[Path, dict[str, str], dict[str, Any], Path, Path]


@pytest.fixture
def candidate(source: Path) -> tuple[Path, dict[str, str]]:
    base = git(source, "rev-parse", "HEAD")
    (source / "seed").unlink()
    for name in ("package/a.py", "tests/a.py", "docs/note.md"):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("value = 1\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-m", "Independent review assignments")
    subject = {
        "repository": "owner/repository",
        "head": git(source, "rev-parse", "HEAD"),
        "base": base,
        "tree": git(source, "rev-parse", "HEAD^{tree}"),
        "task_digest": "a" * 64,
        "producer_id": "author:checkpoint-test",
    }
    return source, subject


@pytest.fixture
def checkpoint_case(candidate: tuple[Path, dict[str, str]], tmp_path: Path) -> CheckpointCase:
    root, subject = candidate
    plan = review_chunks.propose(root, subject, local_review_files, max_paths=2)
    request = {
        "review_plan": plan,
        "review_chunk": {
            "plan_sha256": review_chunks.plan_digest(plan),
            "wave_id": "b" * 32,
            "chunk": copy.deepcopy(plan["chunks"][0]),
        },
    }
    job, retained = tmp_path / "review-job", tmp_path / "checkpoints"
    job.mkdir()
    retained.mkdir()
    case = root, subject, request, job, retained
    _serve(case)
    return case


def _serve(case: CheckpointCase) -> Path:
    root, subject, request, job, _retained = case
    ledger = job / "served.jsonl"
    files = local_review_files.Files(root, subject["head"], subject["base"], ledger)
    files.changed_files()
    for path in request["review_chunk"]["chunk"]["paths"]:
        offset = 0
        while True:
            page = files.read_diff(path, offset)
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
    return ledger


def _certify(case: CheckpointCase) -> dict[str, Any]:
    root, subject, request, job, retained = case
    return review_checkpoints.certify(
        root,
        subject,
        request,
        job,
        REPO_ROOT / "agent_tools/local_review_files.py",
        retained,
    )


def test_certified_chunk_keeps_exact_scope_and_cannot_certify_whole_candidate(
    checkpoint_case: CheckpointCase,
) -> None:
    root, subject, request, job, _retained = checkpoint_case
    original = (job / "served.jsonl").read_bytes()
    result = _certify(checkpoint_case)
    assignment = request["review_chunk"]
    assert result["coverage_complete"] is True
    assert result["review_scope"] == "chunk"
    assert result["plan_sha256"] == review_chunks.plan_digest(request["review_plan"])
    assert result["wave_id"] == assignment["wave_id"]
    assert result["chunk_id"] == assignment["chunk"]["id"]
    assert result["scope_sha256"] == review_chunks.plan_digest(assignment["chunk"])
    assert {"approved", "verdict"}.isdisjoint(result)
    assert (job / "served.jsonl").read_bytes() == original
    assert not (job / "coverage-error.log").exists()
    files = local_review_files.Files(
        root, subject["head"], subject["base"], Path(result["coverage_ledger"])
    )
    with pytest.raises(ValueError, match="complete diff"):
        local_review_files.complete_coverage(files)


def test_checkpoint_survives_job_removal_with_exact_content_hash_and_private_mode(
    checkpoint_case: CheckpointCase,
) -> None:
    _root, _subject, _request, job, retained = checkpoint_case
    original = (job / "served.jsonl").read_bytes()
    result = _certify(checkpoint_case)
    checkpoint = Path(result["coverage_ledger"])
    assert checkpoint.parent == retained
    assert checkpoint.stat().st_mode & 0o777 == 0o600
    shutil.rmtree(job)
    assert checkpoint.read_bytes() == original
    assert result["coverage_ledger_sha256"] == hashlib.sha256(original).hexdigest()
    assert list(retained.glob("*.tmp")) == []


def test_identical_independent_ledgers_retain_distinct_checkpoint_provenance(
    checkpoint_case: CheckpointCase,
) -> None:
    root, subject, request, job, retained = checkpoint_case
    first = _certify(checkpoint_case)
    assert _certify(checkpoint_case) == first
    second_job = job.with_name("independent-review-job")
    second_job.mkdir()
    (second_job / "served.jsonl").write_bytes((job / "served.jsonl").read_bytes())
    second = _certify((root, subject, request, second_job, retained))
    assert first["coverage_complete"]
    assert second["coverage_complete"]
    assert first["coverage_ledger"] != second["coverage_ledger"]
    assert first["coverage_ledger_sha256"] == second["coverage_ledger_sha256"]
    assert (
        Path(first["coverage_ledger"]).read_bytes() == Path(second["coverage_ledger"]).read_bytes()
    )
    assert len(list(retained.glob("*.jsonl"))) == 2


def test_additional_reads_create_a_new_checkpoint_without_rewriting_the_previous_one(
    checkpoint_case: CheckpointCase,
) -> None:
    first = _certify(checkpoint_case)
    original = Path(first["coverage_ledger"]).read_bytes()
    _serve(checkpoint_case)
    second = _certify(checkpoint_case)
    assert second["coverage_complete"] is True
    assert second["coverage_ledger"] != first["coverage_ledger"]
    assert second["coverage_ledger_sha256"] != first["coverage_ledger_sha256"]
    assert Path(first["coverage_ledger"]).read_bytes() == original
    assert Path(second["coverage_ledger"]).read_bytes().startswith(original)


@pytest.mark.parametrize(
    "mutation", ["empty", "malformed", "non_object", "inventory", "missing_diff", "gap", "hash"]
)
def test_uncertified_read_evidence_is_retained_as_incomplete_without_approval(
    checkpoint_case: CheckpointCase, mutation: str
) -> None:
    _root, _subject, _request, job, _retained = checkpoint_case
    ledger = job / "served.jsonl"
    events = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    if mutation == "empty":
        data = ""
    elif mutation == "malformed":
        data = "{not-json\n"
    elif mutation == "non_object":
        data = "[]\n"
    else:
        if mutation == "inventory":
            events = [event for event in events if event["kind"] != "inventory"]
        elif mutation == "missing_diff":
            events = [event for event in events if event["kind"] != "diff"]
        elif mutation == "gap":
            next(event for event in events if event["kind"] == "diff")["offset"] = 1
        else:
            next(event for event in events if event["kind"] == "diff")["sha256"] = "0" * 64
        data = "\n".join(json.dumps(event) for event in events) + "\n"
    ledger.write_text(data, encoding="utf-8")
    original = ledger.read_bytes()
    result = _certify(checkpoint_case)
    assert result["coverage_complete"] is False
    assert {"approved", "verdict"}.isdisjoint(result)
    assert (job / "coverage-error.log").read_text(encoding="utf-8")
    assert ledger.read_bytes() == original
    assert Path(result["coverage_ledger"]).read_bytes() == original
    assert result["coverage_ledger_sha256"] == hashlib.sha256(original).hexdigest()


def test_missing_read_ledger_cannot_produce_a_checkpoint(
    checkpoint_case: CheckpointCase,
) -> None:
    _root, _subject, _request, job, retained = checkpoint_case
    (job / "served.jsonl").unlink()
    with pytest.raises(FileNotFoundError, match=r"served\.jsonl"):
        _certify(checkpoint_case)
    assert list(retained.iterdir()) == []


@pytest.mark.parametrize("mutation", ["digest", "purpose", "paths", "unknown_chunk"])
def test_assignment_cannot_change_its_frozen_scope_or_plan_digest(
    checkpoint_case: CheckpointCase, mutation: str
) -> None:
    root, subject, request, job, retained = checkpoint_case
    changed = copy.deepcopy(request)
    assignment = changed["review_chunk"]
    if mutation == "digest":
        assignment["plan_sha256"] = "0" * 64
    elif mutation == "purpose":
        assignment["chunk"]["purpose"] = "Review a different obligation."
    elif mutation == "paths":
        assignment["chunk"]["paths"] = changed["review_plan"]["chunks"][1]["paths"]
    else:
        assignment["chunk"]["id"] = "unknown-chunk"
    with pytest.raises(ValueError, match=r"assignment.*frozen plan"):
        _certify((root, subject, changed, job, retained))
    assert list(retained.iterdir()) == []
    assert not (job / "coverage-error.log").exists()


@pytest.mark.parametrize("mutation", ["empty_inventory", "task_digest", "empty_author_plan"])
def test_malformed_or_rebound_plan_is_not_checkpointed(
    checkpoint_case: CheckpointCase, mutation: str
) -> None:
    root, subject, request, job, retained = checkpoint_case
    changed = copy.deepcopy(request)
    if mutation == "empty_inventory":
        changed["review_plan"]["inventory"] = []
    elif mutation == "task_digest":
        changed["review_plan"]["subject"]["task_digest"] = "c" * 64
    else:
        changed["review_plan"]["author_plan"] = ""
    changed["review_chunk"]["plan_sha256"] = review_chunks.plan_digest(changed["review_plan"])
    with pytest.raises(ValueError, match=r"inventory|candidate binding|author review plan"):
        _certify((root, subject, changed, job, retained))
    assert list(retained.iterdir()) == []


def test_corrupted_existing_checkpoint_is_never_overwritten(
    checkpoint_case: CheckpointCase,
) -> None:
    _root, _subject, _request, job, retained = checkpoint_case
    ledger = job / "served.jsonl"
    name, _digest = review_checkpoints._persist(retained, ledger)
    checkpoint = Path(name)
    checkpoint.write_bytes(b"retained corruption")
    with pytest.raises(ValueError, match="checkpoint content changed"):
        review_checkpoints._persist(retained, ledger)
    assert checkpoint.read_bytes() == b"retained corruption"
    assert list(retained.glob("*.tmp")) == []


def test_symlinked_checkpoint_is_refused_without_touching_its_target(
    checkpoint_case: CheckpointCase, tmp_path: Path
) -> None:
    _root, _subject, _request, job, retained = checkpoint_case
    ledger = job / "served.jsonl"
    name, _digest = review_checkpoints._persist(retained, ledger)
    checkpoint = Path(name)
    checkpoint.unlink()
    sentinel = tmp_path / "sentinel.jsonl"
    sentinel.write_bytes(b"must survive")
    checkpoint.symlink_to(sentinel)
    with pytest.raises(ValueError, match="Unsafe review coverage checkpoint"):
        review_checkpoints._persist(retained, ledger)
    assert checkpoint.is_symlink()
    assert sentinel.read_bytes() == b"must survive"
    assert list(retained.glob("*.tmp")) == []


def _worker_request() -> dict[str, Any]:
    chunk = {
        "id": "assigned-chunk",
        "paths": ["owned/module.py"],
        "purpose": "Inspect the assigned component and its interaction contract.",
    }
    plan = {
        "author_plan": "PRIVATE_GLOBAL_AUTHOR_PLAN",
        "inventory": [{"path": "owned/module.py"}]
        + [{"path": f"OTHER_UNASSIGNED/path_{index}.py"} for index in range(400)],
        "components": [{"id": "owned"}, {"id": "unassigned"}],
        "chunks": [chunk]
        + [
            {
                "id": f"other-{index}",
                "paths": [f"OTHER_UNASSIGNED/path_{index}.py"],
                "purpose": "PRIVATE_UNASSIGNED_PURPOSE",
            }
            for index in range(400)
        ],
        "limits": {"max_workers": 4, "max_paths": 30, "max_diff_chars": 96000},
    }
    return {
        "subject": {"head": "a" * 40, "task_digest": "b" * 64},
        "discovery_only": True,
        "user_task": {"original": "Full authenticated user task", "corrections": ["Current spec"]},
        "validation": {"current_checks": "Exact candidate check evidence"},
        "evidence": {"visual": "Original allowed image evidence"},
        "candidate_task_attachment": {"sha256": "c" * 64},
        "qualification": "Read-only isolated worker",
        "review_plan": plan,
        "review_chunk": {
            "plan_sha256": review_chunks.plan_digest(plan),
            "wave_id": "d" * 32,
            "chunk": copy.deepcopy(chunk),
        },
        "iteration": {
            "attempt_id": "current-attempt",
            "number": 2,
            "procedure": "discovery-chunks-v3",
            "instructions": "Report all additional current issues in the assigned scope.",
            "excluded_finding_ids": ["open-7"],
            "previous_rounds_sha256": "e" * 64,
            "recorded_findings": [
                {
                    "id": "open-7",
                    "finding": {"title": "CURRENT_OPEN_TEXT", "body": "Current issue details"},
                    "origin": {"private_record": "PRIVATE_ORIGIN_PROVENANCE"},
                }
            ],
            "prior_findings": [{"id": "closed-1", "finding": "PRIVATE_CLOSED_FINDING"}],
            "private_history": "PRIVATE_ITERATION_HISTORY",
        },
        "author_navigation": "PRIVATE_AUTHOR_NAV",
        "private_fix_summary": "PRIVATE_FIXED_HISTORY",
    }


def test_worker_prompt_exposes_only_its_bounded_assignment_and_allowed_task_evidence() -> None:
    request = _worker_request()
    original = copy.deepcopy(request)
    visible = review_checkpoints.worker_input(request)
    serialized = json.dumps(visible, sort_keys=True)
    for private in (
        "OTHER_UNASSIGNED",
        "PRIVATE_UNASSIGNED_PURPOSE",
        "PRIVATE_GLOBAL_AUTHOR_PLAN",
        "PRIVATE_AUTHOR_NAV",
        "PRIVATE_FIXED_HISTORY",
    ):
        assert private not in serialized
    assert "review_plan" not in visible
    assert visible["review_chunk"] == request["review_chunk"]
    assert visible["review_plan_summary"] == {
        "plan_sha256": review_chunks.plan_digest(request["review_plan"]),
        "inventory_sha256": review_chunks.plan_digest(request["review_plan"]["inventory"]),
        "inventory_paths": 401,
        "component_count": 2,
        "chunk_count": 401,
        "limits": request["review_plan"]["limits"],
    }
    for key in (
        "subject",
        "discovery_only",
        "user_task",
        "validation",
        "evidence",
        "candidate_task_attachment",
        "qualification",
    ):
        assert visible[key] == request[key]
    assert request == original


def test_worker_prompt_has_one_current_open_exclusion_without_private_finding_history() -> None:
    request = _worker_request()
    original = copy.deepcopy(request)
    visible = review_checkpoints.worker_input(request)
    iteration = visible["iteration"]
    assert iteration["recorded_findings"] == [
        {"id": "open-7", "finding": request["iteration"]["recorded_findings"][0]["finding"]}
    ]
    assert iteration["excluded_finding_ids"] == ["open-7"]
    assert (
        iteration["prior_findings_sha256"]
        == hashlib.sha256(
            json.dumps(
                request["iteration"]["prior_findings"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    assert "prior_findings" not in iteration
    serialized = json.dumps(visible)
    assert serialized.count("CURRENT_OPEN_TEXT") == 1
    for private in (
        "PRIVATE_CLOSED_FINDING",
        "PRIVATE_ORIGIN_PROVENANCE",
        "PRIVATE_ITERATION_HISTORY",
    ):
        assert private not in serialized
    assert request == original


def test_legacy_worker_input_without_chunk_preserves_the_original_request() -> None:
    request = {"user_task": {"original": "Legacy task"}, "legacy_evidence": ["Retain shape"]}
    original = copy.deepcopy(request)
    assert review_checkpoints.worker_input(request) is request
    assert request == original
