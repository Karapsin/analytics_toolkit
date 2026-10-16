"""Activate approved automation paths without promoting package code or releases."""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


def stamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def blob(controller: Any, work: Path, revision: str, path: str) -> tuple[str, str] | None:
    raw = controller.command(
        "git",
        "--literal-pathspecs",
        "ls-tree",
        "-z",
        revision,
        "--",
        path,
        cwd=work,
        raw_stdout=True,
    )
    if not raw:
        return None
    metadata, name = raw.rstrip(b"\0").split(b"\t", 1)
    mode, kind, oid = metadata.decode("ascii").split()
    if name.decode() != path or kind != "blob" or mode not in {"100644", "100755"}:
        raise ValueError("Automation activation requires regular source files")
    return mode, oid


def project(controller: Any, work: Path, head: str, previous_dev: str, paths: list[str]) -> bool:
    """Apply only approved automation endpoints; a newer main edit wins."""
    changed = False
    for path in paths:
        if not controller.policy_path(path):
            raise ValueError("Package paths cannot enter automation activation")
        current = blob(controller, work, "HEAD", path)
        candidate = blob(controller, work, head, path)
        if current == candidate:
            continue
        if current != blob(controller, work, previous_dev, path):
            raise ValueError("Main automation changed independently; fresh review required")
        if candidate is None:
            controller.command("git", "update-index", "--force-remove", "--", path, cwd=work)
        else:
            controller.command(
                "git",
                "update-index",
                "--add",
                "--cacheinfo",
                candidate[0],
                candidate[1],
                path,
                cwd=work,
            )
        changed = True
    return changed


def run(controller: Any, gh: Any, app_id: int, checkout: Path) -> dict[str, Any]:
    state = Path(os.environ["AGENT_STATE_DIR"]) / "automation-activation.json"
    value = (
        json.loads(state.read_text()) if state.exists() else {"started": time.time(), "done": []}
    )
    result: dict[str, Any] = {"status": "idle"}
    try:
        pulls = gh.pages("pulls?state=closed&base=dev&sort=updated&direction=desc&per_page=100")
        candidates = [
            pr for pr in pulls if pr.get("merged_at") and stamp(pr["merged_at"]) >= value["started"]
        ]
        for listed in sorted(candidates, key=lambda pr: (stamp(pr["merged_at"]), pr["number"])):
            pr = gh.pull(listed["number"])
            identity = str(pr["number"]) + ":" + pr["head"]["sha"]
            value["pending"] = {"pr": pr["number"], "head": pr["head"]["sha"]}
            if identity in value["done"]:
                continue
            files = gh.pages(f"pulls/{pr['number']}/files?per_page=100")
            paths = sorted(
                {
                    path
                    for item in files
                    for path in (item["filename"], item.get("previous_filename", ""))
                    if controller.policy_path(path)
                }
            )
            if not paths:
                value["done"].append(identity)
                continue
            # Reuse enrolled-signature and actual owner exact-head approval checks;
            # a merged PR is normalized only for their open-PR trust predicate.
            candidate = dict(pr, state="open")
            if (
                pr.get("base", {}).get("ref") != "dev"
                or not pr.get("merged")
                or controller.authorization_error(gh, candidate) is not None
            ):
                raise ValueError("Automation activation requires the authenticated owner decision")
            task = controller.task_metadata(gh, candidate)
            head = pr["head"]["sha"]
            merge = pr["merge_commit_sha"]
            dev = gh.api("git/ref/heads/dev")["object"]["sha"]
            controller.command("git", "fetch", "origin", merge, head, dev, cwd=checkout)
            parents = controller.command(
                "git", "rev-list", "--parents", "-n", "1", merge, cwd=checkout
            ).split()
            if len(parents) not in {2, 3} or (len(parents) == 3 and parents[2] != head):
                raise ValueError("Automation activation requires the approved dev merge")
            controller.command("git", "merge-base", "--is-ancestor", merge, dev, cwd=checkout)
            previous = parents[1]
            # GitHub uses squash merges here. Bind the approved head to the
            # recorded result, including unrelated dev changes since branching.
            expected_tree = controller.command(
                "git", "merge-tree", "--write-tree", previous, head, cwd=checkout
            ).splitlines()[0]
            merged_tree = controller.command("git", "rev-parse", merge + "^{tree}", cwd=checkout)
            if expected_tree != merged_tree:
                raise ValueError("Merged automation tree does not match the approved head")
            checks = gh.checks(head)
            fast = [
                c
                for c in checks
                if c["name"] == "fast-checks" and c.get("app", {}).get("slug") == "github-actions"
            ]
            review = [
                c
                for c in checks
                if c["name"] == "agent-review" and c.get("app", {}).get("id") == app_id
            ]
            latest_fast = max(fast, key=lambda c: c.get("id", 0)) if fast else {}
            latest_review = max(review, key=lambda c: c.get("id", 0)) if review else {}
            if not (
                latest_fast.get("conclusion") == "success"
                and latest_review.get("conclusion") == "success"
                and latest_review.get("external_id") == previous
            ):
                raise ValueError("Automation activation needs exact-head and merge-base checks")
            main = gh.api("git/ref/heads/main")["object"]["sha"]
            work = state.parent / ("automation-activation-" + uuid.uuid4().hex)
            try:
                controller.command(
                    "git", "clone", "--shared", "--no-checkout", str(checkout), str(work)
                )
                controller.command(
                    "git",
                    "remote",
                    "set-url",
                    "origin",
                    "https://github.com/" + gh.repository + ".git",
                    cwd=work,
                )
                controller.command("git", "fetch", "origin", main, cwd=work)
                controller.command(
                    "git", "-c", "core.hooksPath=/dev/null", "checkout", "--detach", main, cwd=work
                )
                if project(controller, work, merge, previous, paths):
                    if gh.api("git/ref/heads/main")["object"]["sha"] != main:
                        raise ValueError("Main moved; activation retained for retry")
                    tree = controller.command("git", "write-tree", cwd=work)
                    commit = controller.command(
                        "git",
                        *controller.signing_options(),
                        "commit-tree",
                        "-S",
                        tree,
                        "-p",
                        main,
                        "-m",
                        f"Activate owner-approved automation from PR #{pr['number']} ({head})",
                        cwd=work,
                    )
                    latest = gh.pull(pr["number"])
                    refreshed = dict(latest, state="open")
                    if (
                        latest.get("head", {}).get("sha") != head
                        or latest.get("merge_commit_sha") != merge
                        or latest.get("base", {}).get("ref") != "dev"
                        or not latest.get("merged")
                        or controller.authorization_error(gh, refreshed) is not None
                        or controller.task_metadata(gh, refreshed) != task
                    ):
                        raise ValueError("Automation authorization changed during staging")
                    controller.command(
                        "git", "push", "origin", commit + ":refs/heads/main", cwd=work
                    )
                    result = {
                        "status": "activated",
                        "pr": pr["number"],
                        "head": head,
                        "main": commit,
                    }
                value["done"].append(identity)
            finally:
                if work.exists():
                    shutil.rmtree(work)
        value.pop("error", None)
        value.pop("pending", None)
    except Exception as error:
        # Preserve the failed identity for retry, without exposing credentials or
        # allowing activation failure to block maintenance/ordinary PR work.
        value["error"] = type(error).__name__
        safe_reasons = {
            "Automation activation requires regular source files",
            "Package paths cannot enter automation activation",
            "Main automation changed independently; fresh review required",
            "Automation activation requires the authenticated owner decision",
            "Automation activation needs exact-head and merge-base checks",
            "Automation activation requires the approved dev merge",
            "Merged automation tree does not match the approved head",
            "Automation authorization changed during staging",
            "Main moved; activation retained for retry",
        }
        if str(error) in safe_reasons:
            value["reason"] = str(error)
        result = {"status": "deferred", "error": type(error).__name__}
    finally:
        temporary = state.with_name(state.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(value))
        temporary.replace(state)
    return result
