"""Delete unchanged merged feature refs; preserve unknown and active work."""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any


def _git(work: Path, *args: str) -> str:
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=work,
                            env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null"),
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError("Branch cleanup deferred; exact-head deletion did not complete.")
    return result.stdout.strip()


def _eligible(pr: dict[str, Any], repository: str) -> bool:
    spec = importlib.util.spec_from_file_location("cleanup_controller", Path(__file__).with_name("controller.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("Trusted cleanup provenance verifier is unavailable.")
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    candidate = dict(pr, state="open", draft=False)
    return controller.trusted(candidate, repository) and controller.signed_head(repository, pr["head"]["sha"])


def cleanup(gh: Any, work: Path, *, active_branches: set[str] | None = None,
            force: bool = False) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("cleanup_controls", Path(__file__).with_name("review_control.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("Trusted cleanup state is unavailable.")
    controls = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controls)
    key = gh.repository + ":branch-cleanup"
    last = float(controls.cursor(key, "0"))
    if not force and time.time() - last < 86400:
        return {"deleted": [], "preserved": [], "status": "not_due"}
    active = set(active_branches or ())
    opened = gh.pulls()
    protected = {"dev", "main"} | active | {pr["head"]["ref"] for pr in opened}
    branches = {item["name"]: item for item in gh.pages("branches?per_page=100")}
    merged: dict[str, dict[str, Any]] = {}
    for pr in gh.pages("pulls?state=closed&base=dev&sort=updated&direction=desc&per_page=100"):
        if pr.get("merged_at") and (pr["head"].get("repo") or {}).get("full_name") == gh.repository:
            merged.setdefault(pr["head"]["ref"], pr)
    deleted, preserved = [], []
    for branch, item in sorted(branches.items()):
        pr = merged.get(branch)
        head = item["commit"]["sha"]
        if (branch in protected or item.get("protected") or pr is None
                or not re.fullmatch(r"codex/[A-Za-z0-9._/-]+", branch)
                or not re.fullmatch(r"[0-9a-f]{40}", head)
                or head != pr["head"]["sha"]):
            preserved.append(branch)
            continue
        current = gh.pull(pr["number"])
        if not current.get("merged_at") or current["head"]["sha"] != head or not _eligible(current, gh.repository):
            preserved.append(branch)
            continue
        # Detect branch reuse by a new open PR immediately before the leased delete.
        if any(candidate["head"]["ref"] == branch for candidate in gh.pulls()):
            preserved.append(branch)
            continue
        ref = "refs/heads/" + branch
        _git(work, "check-ref-format", ref)
        remote = _git(work, "ls-remote", "origin", ref)
        if not remote or remote.split()[0] != head:
            preserved.append(branch)
            continue
        _git(work, "push", f"--force-with-lease={ref}:{head}", "origin", ":" + ref)
        controls.set_cursor(key + ":" + branch + ":" + head, str(pr["number"]))
        deleted.append(branch)
    controls.set_cursor(key, str(time.time()))
    return {"deleted": deleted, "preserved": preserved, "status": "completed"}
