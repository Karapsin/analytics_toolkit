"""Conservative cleanup of inactive, proven merged local feature branches."""

from __future__ import annotations

import importlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

sessions = importlib.import_module(f"{__package__}.sessions" if __package__ else "sessions")


def cleanup(root: Path) -> dict[str, Any]:
    """Unknown/unpublished branches are preserved; Git refuses checked-out deletion."""
    removed: list[str] = []
    preserved: list[str] = []
    try:
        sessions.clean(root)
        sessions.run(root, "git", "fetch", "--prune", "origin")
        branches = sessions.run(
            root,
            "git",
            "for-each-ref",
            "--format=%(refname:short) %(objectname)",
            "refs/heads/codex/",
        )
        if not branches:
            return {"removed": [], "preserved": []}
        merged = json.loads(
            sessions.run(
                root,
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--base",
                "dev",
                "--limit",
                "1000",
                "--json",
                "headRefName,headRefOid,number",
            )
        )
        proofs = {(item["headRefName"], item["headRefOid"]) for item in merged}
        occupied = {
            line[len("branch refs/heads/") :]
            for line in sessions.run(root, "git", "worktree", "list", "--porcelain").splitlines()
            if line.startswith("branch refs/heads/")
        }
        for record in branches.splitlines():
            branch, head = record.split(" ", 1)
            if (
                branch in occupied
                or branch.startswith(("codex/backup/", "codex/archive/"))
                or (branch, head) not in proofs
            ):
                preserved.append(branch)
                continue
            # Keep squash-only local history unless it is also reachable from dev.
            # Remote cleanup uses the stronger merged-PR receipt; local -d is deliberately safer.
            try:
                sessions.run(root, "git", "merge-base", "--is-ancestor", head, "dev")
                if sessions.run(root, "git", "rev-parse", branch) != head:
                    preserved.append(branch)
                    continue
                sessions.run(root, "git", "branch", "-d", branch)
                removed.append(branch)
            except RuntimeError:
                preserved.append(branch)
    except (RuntimeError, ValueError, OSError) as exc:
        return {"removed": removed, "preserved": preserved, "pending": str(exc)}
    return {"removed": removed, "preserved": preserved}
