"""Synthetic reviewer evidence with real temporary SSH signatures; no model calls."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any

from agent_tools import (
    local_review,
    local_review_files,
    review_chunks,
    review_iterations,
    review_waves,
    sessions,
    task_context,
)
from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path


def signed_receipt(root: Path, repository: str = "owner/repo") -> dict[str, Any]:
    bootstrap = sessions.state_path(root).with_name("atk-local-review-bootstrap")
    bootstrap.mkdir(exist_ok=True)
    names = (
        "agent_tools/local_review_files.py",
        ".github/agent/review.schema.json",
        ".github/agent/local_review.py",
    )
    manifest = {"reviewer_id": "synthetic-bootstrap-reviewer", "files": {}}
    for name in names:
        data = (REPO_ROOT / name).read_bytes()
        (bootstrap / local_review.Path(name).name).write_bytes(data)
        manifest["files"][name] = hashlib.sha256(data).hexdigest()
    (bootstrap / "manifest.json").write_text(json.dumps(manifest))
    proto = local_review.protocol(root)
    context = task_context.protocol().public_context(task_context.read(root))
    head = sessions.run(root, "git", "rev-parse", "HEAD")
    payload = {
        "version": 1,
        "kind": "local-source-review",
        "mode": "read-only",
        "repository": repository,
        "head": head,
        "base": sessions.load(root)["base"],
        "tree": sessions.run(root, "git", "rev-parse", "HEAD^{tree}"),
        "task_digest": task_context.protocol().digest(context),
        "producer_id": task_context.protocol().verify_commit(
            context, sessions.run(root, "git", "log", "-1", "--format=%B")
        ),
        "reviewer_id": "reviewer:" + str(uuid.uuid4()),
        "approved": True,
        "alignment": "aligned",
        "summary": "Synthetic independent reviewer approval",
        "execution_sha256": "a" * 64,
    }
    subject = {
        k: payload[k] for k in ("repository", "head", "base", "tree", "task_digest", "producer_id")
    }
    rounds = _signed_waves(root, subject)
    payload["reviewer_id"] = rounds[-1]["payload"]["reviewer_id"]
    payload["execution_sha256"] = rounds[-1]["payload"]["execution_sha256"]
    payload["review_iterations"] = review_iterations.compact_proof(root, rounds, subject)
    job = local_review.directory(root) / ("fixture-" + uuid.uuid4().hex)
    job.mkdir()
    value = local_review._sign_payload(root, job, payload)
    (local_review.directory(root) / (head + ".json")).write_bytes(proto.canonical(value))
    return value


def _signed_waves(root, subject):
    manifest = review_chunks.load(root, subject, local_review_files)
    history = review_iterations.history(root)
    previous = [attempt["result"] for attempt in history["attempts"] if "result" in attempt]
    known = review_iterations.recorded_findings(previous, subject)
    iteration = {
        "attempt_id": uuid.uuid4().hex,
        "number": 1,
        "minimum_completed_rounds": 1,
        "procedure": "discovery-chunks-v4",
        "discovery_only": False,
        "wave_id": uuid.uuid4().hex,
        "instructions": review_iterations.INSTRUCTIONS,
        "prior_findings": review_iterations._findings(previous, subject),
        "recorded_findings": known,
        "excluded_finding_ids": [finding["id"] for finding in known],
        "previous_rounds_sha256": review_iterations.findings_digest(previous),
    }
    return [_signed_wave(root, subject, manifest, iteration)]


def _signed_wave(root, subject, manifest, iteration):
    children = [
        signed_round(
            root,
            subject,
            modern=True,
            review_chunk={
                "plan_sha256": review_chunks.plan_digest(manifest),
                "wave_id": iteration["wave_id"],
                "chunk": chunk,
            },
            prior_findings=iteration["prior_findings"],
            recorded_findings=iteration["recorded_findings"],
            previous_rounds_sha256=iteration["previous_rounds_sha256"],
        )
        for chunk in manifest["chunks"]
    ]
    review_chunks.aggregate_coverage(
        local_review_files,
        root,
        subject,
        manifest,
        {
            child["payload"]["chunk_id"]: local_review.Path(child["payload"]["coverage_ledger"])
            for child in children
        },
    )
    job = local_review.directory(root) / ("fixture-wave-" + uuid.uuid4().hex)
    job.mkdir()
    receipt = local_review._sign_payload(
        root, job, review_waves.aggregate_payload(subject, iteration, manifest, children, True)
    )
    state = review_waves._read(root)
    state["waves"].append(
        {
            "id": iteration["wave_id"],
            "subject": subject,
            "manifest": manifest,
            "plan_sha256": review_chunks.plan_digest(manifest),
            "discovery_only": False,
            "iteration": iteration,
            "outcomes": [
                {
                    "chunk_id": child["payload"]["chunk_id"],
                    "attempt_id": uuid.uuid4().hex,
                    "result": child,
                }
                for child in children
            ],
            "complete": True,
        }
    )
    review_waves._save(root, state)
    history = review_iterations.history(root)
    history["attempts"].append(
        {
            "id": iteration["attempt_id"],
            "subject": subject,
            "iteration": iteration,
        }
    )
    review_iterations._save(root, history)
    review_iterations.record(root, iteration["attempt_id"], receipt)
    return receipt


def signed_round(  # noqa: PLR0913 - explicit independent-review outcome fixture.
    root,
    subject,
    *,
    findings=None,
    approved=True,
    coverage=True,
    prior_findings=None,
    reviewer_id=None,
    previous_rounds_sha256=None,
    modern=False,
    discovery_only=False,
    recorded_findings=None,
    alignment=None,
    summary=None,
    review_chunk=None,
    procedure=None,
):
    """Synthetic round provenance with actual temporary-key signatures."""
    job = local_review.directory(root) / ("fixture-round-" + uuid.uuid4().hex)
    job.mkdir()
    payload = {
        "version": 1,
        "kind": "local-review-round",
        **subject,
        "reviewer_id": reviewer_id or "reviewer:" + str(uuid.uuid4()),
        "reviewer_runtime": "codex",
        "reviewer_version": "fixture",
        "reviewer_model": "codex-default",
        "reviewer_auth": "chatgpt",
        "execution_sha256": hashlib.sha256(job.name.encode()).hexdigest(),
        "coverage_complete": coverage,
        "prior_findings_sha256": review_iterations.findings_digest(prior_findings or []),
        "previous_rounds_sha256": previous_rounds_sha256 or review_iterations.findings_digest([]),
        "verdict": {
            "approved": approved,
            "alignment": alignment or ("not_aligned" if findings else "aligned"),
            "summary": summary or "Synthetic reviewer result",
            "change_summary": "Feature",
            "findings": findings or [],
        },
    }
    if modern:
        payload.update(
            review_procedure="discovery-batch-v2",
            discovery_only=discovery_only,
            recorded_findings_sha256=review_iterations.findings_digest(recorded_findings or []),
        )
    if review_chunk is not None:
        chunk = review_chunk["chunk"]
        ledger = job / "read-ledger.jsonl"
        files = local_review_files.Files(root, subject["head"], subject["base"], ledger)
        files.changed_files()
        if coverage:
            for path in chunk["paths"]:
                offset = 0
                while True:
                    page = files.read_diff(path, offset)
                    if page["next_offset"] is None:
                        break
                    offset = page["next_offset"]
        payload.update(
            review_procedure="discovery-chunks-v4",
            review_scope="chunk",
            plan_sha256=review_chunk["plan_sha256"],
            wave_id=review_chunk["wave_id"],
            chunk_id=chunk["id"],
            scope_sha256=hashlib.sha256(local_review.protocol(root).canonical(chunk)).hexdigest(),
            coverage_ledger=str(ledger),
            coverage_ledger_sha256=hashlib.sha256(ledger.read_bytes()).hexdigest(),
        )
    if procedure is not None:
        payload["review_procedure"] = procedure
    return local_review._sign_payload(root, job, payload)
