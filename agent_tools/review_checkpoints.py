"""Durable, scope-bound coverage checkpoints for isolated review workers."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any

from agent_tools import review_chunks

if TYPE_CHECKING:
    from pathlib import Path


def worker_input(request: dict[str, Any]) -> dict[str, Any]:
    """Keep one assignment and current exclusions in the reviewer's actual prompt."""
    if "review_chunk" not in request:
        return request
    plan = request["review_plan"]
    iteration = request["iteration"]
    visible = {
        key: request[key]
        for key in (
            "subject",
            "discovery_only",
            "user_task",
            "validation",
            "evidence",
            "candidate_task_attachment",
            "qualification",
            "review_chunk",
        )
        if key in request
    }
    visible["review_plan_summary"] = {
        "plan_sha256": review_chunks.plan_digest(plan),
        "inventory_sha256": review_chunks.plan_digest(plan["inventory"]),
        "inventory_paths": len(plan["inventory"]),
        "component_count": len(plan["components"]),
        "chunk_count": len(plan["chunks"]),
        "limits": plan["limits"],
    }
    visible["iteration"] = {
        key: iteration[key]
        for key in (
            "attempt_id",
            "number",
            "historical_attempt_number",
            "minimum_completed_rounds",
            "procedure",
            "discovery_only",
            "instructions",
            "excluded_finding_ids",
            "previous_rounds_sha256",
        )
        if key in iteration
    }
    visible["iteration"]["recorded_findings"] = [
        {"id": item["id"], "finding": item["finding"]} for item in iteration["recorded_findings"]
    ]
    visible["iteration"]["prior_findings_sha256"] = hashlib.sha256(
        json.dumps(iteration["prior_findings"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return visible


def _persist(directory: Path, ledger: Path) -> tuple[str, str]:
    data = ledger.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    owner = hashlib.sha256(str(ledger.resolve()).encode()).hexdigest()[:16]
    target = directory / ("coverage-" + owner + "-" + digest + ".jsonl")
    if target.is_symlink():
        msg = "Unsafe review coverage checkpoint"
        raise ValueError(msg)
    if target.exists():
        if target.read_bytes() != data:
            msg = "Review coverage checkpoint content changed"
            raise ValueError(msg)
    else:
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(data)
            temporary.chmod(0o600)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return str(target), digest


def certify(  # noqa: PLR0913 - trusted binding stays separate from the worker request.
    root: Path,
    subject: dict[str, str],
    request: dict[str, Any],
    job: Path,
    source: Path,
    directory: Path,
) -> dict[str, Any]:
    """Verify one complete assignment without certifying the entire candidate."""
    plan = request["review_plan"]
    assignment = request["review_chunk"]
    review_chunks.validate_manifest(subject, plan)
    chunk = assignment["chunk"]
    matches = [item for item in plan["chunks"] if item["id"] == chunk["id"]]
    if matches != [chunk] or assignment["plan_sha256"] != review_chunks.plan_digest(plan):
        msg = "Review worker assignment does not match its frozen plan"
        raise ValueError(msg)
    ledger = job / "served.jsonl"
    complete = True
    try:
        review_chunks.scoped_coverage(
            review_chunks.load_provider(source), root, subject, plan, chunk["id"], ledger
        )
    except (OSError, ValueError) as exc:
        complete = False
        (job / "coverage-error.log").write_text(str(exc), encoding="utf-8")
    retained, digest = _persist(directory, ledger)
    return {
        "review_scope": "chunk",
        "plan_sha256": assignment["plan_sha256"],
        "wave_id": assignment["wave_id"],
        "chunk_id": chunk["id"],
        "scope_sha256": review_chunks.plan_digest(chunk),
        "coverage_complete": complete,
        "coverage_ledger": retained,
        "coverage_ledger_sha256": digest,
    }
