"""Narrow Pi-fix fraud check: detect test/CI cheating only, never judge design.

The Pi runs no source, plan or visual review. Its only model gate is this
read-only check of Pi-authored fix heads (conflict, correction, repair,
metadata and feature commits). The verdict is accept or reject with a reason.
Verdicts are stored privately and bound to the exact PR, head, base and task.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

KIND = "fraud-review"
PI_KINDS = ("conflict", "correction", "repair", "metadata", "feature")
DECISIONS = frozenset({"accept", "reject"})
MAX_REASON = 4000
MAX_FINDINGS = 50


def connect() -> sqlite3.Connection:
    directory = Path(os.environ["AGENT_STATE_DIR"])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "fraud_review.sqlite3"
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS fraud_verdicts (
        repository TEXT NOT NULL, number INTEGER NOT NULL, head TEXT NOT NULL,
        base TEXT NOT NULL, digest TEXT NOT NULL, decision TEXT NOT NULL,
        reason TEXT NOT NULL, created REAL NOT NULL,
        PRIMARY KEY(repository, number, head, digest))""")
    path.chmod(0o600)
    return db


def validate(verdict: Any) -> dict[str, Any]:
    """Accept only the exact fraud-review schema; anything else fails closed."""
    if (not isinstance(verdict, dict) or set(verdict) != {"decision", "reason", "findings"}
            or verdict["decision"] not in DECISIONS
            or not isinstance(verdict["reason"], str) or not verdict["reason"].strip()
            or not isinstance(verdict["findings"], list) or len(verdict["findings"]) > MAX_FINDINGS
            or any(not isinstance(item, dict) or set(item) != {"path", "issue"}
                   or not isinstance(item["path"], str) or not isinstance(item["issue"], str)
                   for item in verdict["findings"])):
        raise ValueError("Malformed fraud-review verdict")
    if verdict["decision"] == "reject" and not verdict["findings"]:
        raise ValueError("A fraud-review rejection must name concrete findings")
    return verdict


def explanation(verdict: dict[str, Any]) -> str:
    lines = [verdict["reason"].strip()[:MAX_REASON]]
    lines.extend("- " + item["path"] + ": " + item["issue"] for item in verdict["findings"])
    return "\n".join(lines)[:60000]


def current(repository: str, number: int, head: str, digest: str) -> dict[str, Any] | None:
    if not os.environ.get("AGENT_STATE_DIR"):
        return None  # No private state means no verdict; nothing is ever accepted by default.
    with connect() as db:
        row = db.execute("SELECT * FROM fraud_verdicts WHERE repository=? AND number=? AND head=? AND digest=?",
                         (repository, number, head, digest)).fetchone()
    return dict(row) if row else None


def record(repository: str, number: int, head: str, base: str, digest: str,
           decision: str, reason: str) -> bool:
    """Insert once; a conflicting later decision for the same head is refused."""
    if decision not in DECISIONS:
        raise ValueError("Unsupported fraud-review decision")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT decision FROM fraud_verdicts WHERE repository=? AND number=? AND head=? AND digest=?",
                         (repository, number, head, digest)).fetchone()
        if row is not None:
            return bool(row["decision"] == decision)
        db.execute("INSERT INTO fraud_verdicts VALUES (?,?,?,?,?,?,?,?)",
                   (repository, number, head, base, digest, decision, reason, time.time()))
    return True
