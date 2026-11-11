"""Synthetic reviewer evidence with real temporary SSH signatures; no model calls."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any

from agent_tools import local_review, review_iterations, sessions, task_context
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
    first = signed_round(root, subject)
    rounds = [
        first,
        signed_round(
            root, subject, previous_rounds_sha256=review_iterations.findings_digest([first])
        ),
    ]
    payload["reviewer_id"] = rounds[-1]["payload"]["reviewer_id"]
    payload["execution_sha256"] = rounds[-1]["payload"]["execution_sha256"]
    payload["review_iterations"] = {"version": 1, "rounds": rounds}
    job = local_review.directory(root) / ("fixture-" + uuid.uuid4().hex)
    job.mkdir()
    value = local_review._sign_payload(root, job, payload)
    (local_review.directory(root) / (head + ".json")).write_bytes(proto.canonical(value))
    return value


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
            "alignment": "not_aligned" if findings else "aligned",
            "summary": "Synthetic reviewer result",
            "change_summary": "Feature",
            "findings": findings or [],
        },
    }
    return local_review._sign_payload(root, job, payload)
