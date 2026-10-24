"""Synthetic reviewer evidence with real temporary SSH signatures; no model calls."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from typing import TYPE_CHECKING, Any

from agent_tools import local_review, sessions, task_context
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
    source = local_review.directory(root) / "fixture-payload"
    source.write_bytes(proto.canonical(payload))
    source.with_name(source.name + ".sig").unlink(missing_ok=True)
    subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "sign",
            "-f",
            sessions.load(root)["signing_key"],
            "-n",
            proto.NAMESPACE,
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    value = {"payload": payload, "signature": source.with_name(source.name + ".sig").read_text()}
    (local_review.directory(root) / (head + ".json")).write_bytes(proto.canonical(value))
    return value
