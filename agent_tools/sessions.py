"""Independent local feature sessions; no public package dependencies."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

STATE = "atk-session.json"
OPERATIONS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "rebase-merge",
    "rebase-apply",
    "sequencer",
    "BISECT_LOG",
)


def run(root: Path, *args: str) -> str:
    result = subprocess.run(args, cwd=root, text=True, capture_output=True, check=False)
    if result.returncode:
        msg = f"{args[0]} {args[1]} failed: {result.stderr.strip()[:2000]}"
        raise RuntimeError(msg)
    return result.stdout.strip()


def state_path(root: Path) -> Path:
    return Path(run(root, "git", "rev-parse", "--absolute-git-dir")) / STATE


def load(root: Path) -> dict[str, Any]:
    # Ordinary checkouts retain the bootstrap/release workflows.
    path = root / ".git" / STATE
    return json.loads(path.read_text()) if path.is_file() else {}


def save(root: Path, state: dict[str, Any]) -> None:
    path = state_path(root)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def commit_options(root: Path) -> list[str]:
    state = load(root)
    if not state:
        return []
    key = state.get("signing_key")
    if not key or not Path(key).is_file():
        msg = "Feature sessions require an enrolled machine signing key."
        raise RuntimeError(msg)
    return ["-c", "gpg.format=ssh", "-c", "user.signingkey=" + key, "-c", "commit.gpgsign=true"]


def clean(root: Path) -> None:
    if run(
        root, "git", "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"
    ):
        msg = "Checkout is dirty; preserve local work before synchronizing."
        raise RuntimeError(msg)
    operation_free(root)


def operation_free(root: Path) -> None:
    git_dir = Path(run(root, "git", "rev-parse", "--absolute-git-dir"))
    if any((git_dir / name).exists() for name in OPERATIONS):
        msg = "Unfinished Git operation; preserve it and resolve explicitly."
        raise RuntimeError(msg)


def sync_dev(root: Path) -> str:
    clean(root)
    run(root, "git", "fetch", "origin", "dev")
    run(root, "git", "switch", "dev")
    run(root, "git", "pull", "--ff-only", "origin", "dev")
    return run(root, "git", "rev-parse", "HEAD")


def create(source: Path, directory: Path) -> Path:
    remote = run(source, "git", "remote", "get-url", "origin")
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    session_id = uuid.uuid4().hex[:16]
    root = directory / session_id
    run(source, "git", "clone", "--branch", "dev", "--single-branch", remote, str(root))
    base = sync_dev(root)
    save(
        root,
        {
            "id": session_id,
            "source": str(source.resolve()),
            "phase": "launching",
            "base": base,
            "branch": "dev",
        },
    )
    return root


def start(root: Path, acknowledged: str | None = None) -> dict[str, Any]:
    state = load(root)
    if state.get("phase") not in {"planning", "revalidate"}:
        msg = "Start requires a launcher session in planning/revalidate phase."
        raise RuntimeError(msg)
    if run(root, "git", "branch", "--show-current") != "dev":
        msg = "Planning session must be on dev before starting a new feature."
        raise RuntimeError(msg)
    baseline = state["base"]
    if acknowledged:
        if state.get("pending_base") != acknowledged:
            msg = "Acknowledgement does not match the last refresh receipt."
            raise RuntimeError(msg)
        baseline = acknowledged
    latest = sync_dev(root)
    if latest != baseline:
        changes = run(root, "git", "diff", "--name-status", baseline, latest)
        state.update(phase="revalidate", pending_base=latest)
        save(root, state)
        return {
            "status": "revalidate",
            "before": baseline,
            "after": latest,
            "changes": changes,
            "next": "Assess impact on the approved plan. Obtain approval "
            "for material revisions, then call start with sha=after to acknowledge.",
        }
    branch = f"codex/{state['id']}-{uuid.uuid4().hex[:8]}"
    run(root, "git", "switch", "-c", branch)
    state.update(phase="feature", branch=branch, base=latest)
    state.pop("pending_base", None)
    state.pop("pr", None)
    save(root, state)
    return {"status": "feature", "branch": branch, "base": latest}


def pr_info(root: Path) -> dict[str, Any]:
    state = load(root)
    target = str(state.get("pr") or state["branch"])
    return json.loads(
        run(
            root,
            "gh",
            "pr",
            "view",
            target,
            "--json",
            "number,url,state,mergedAt,headRefOid,labels,reviews,statusCheckRollup",
        )
    )


def writable(root: Path) -> dict[str, Any]:
    state = load(root)
    branch = run(root, "git", "branch", "--show-current")
    if state.get("phase") != "feature" or branch != state.get("branch"):
        msg = "Feature writes require the session-owned feature branch."
        raise RuntimeError(msg)
    if state.get("pr"):
        pr = pr_info(root)
        if pr["state"] != "OPEN":
            msg = "PR is closed; synchronize before starting another feature."
            raise RuntimeError(msg)
        if any(label["name"] == "agent:writing" for label in pr["labels"]):
            msg = "GitHub agent owns this branch; wait for feedback before editing."
            raise RuntimeError(msg)
        remote = pr["headRefOid"]
        run(root, "git", "fetch", "origin", state["branch"])
        if run(root, "git", "merge-base", "--is-ancestor", remote, "HEAD"):
            msg = "Unexpected ancestry result."
            raise RuntimeError(msg)
    return state


def push(root: Path) -> dict[str, Any]:
    state = writable(root)
    clean(root)
    head = run(root, "git", "rev-parse", "HEAD")
    run(root, "git", "push", "--set-upstream", "origin", f"HEAD:{state['branch']}")
    if not state.get("pr"):
        # Discover an existing PR after an interrupted create/push before creating another.
        matches = json.loads(
            run(
                root,
                "gh",
                "pr",
                "list",
                "--head",
                state["branch"],
                "--base",
                "dev",
                "--state",
                "open",
                "--json",
                "number",
            )
        )
        if matches:
            state["pr"] = matches[0]["number"]
        else:
            body = state_path(root).with_name("atk-pr-body.md")
            body.write_text(
                "Implemented in an isolated Codex session. Local focused and "
                "mandatory pre-commit checks passed. Awaiting agent review and fast CI.\n"
            )
            run(
                root,
                "gh",
                "pr",
                "create",
                "--base",
                "dev",
                "--head",
                state["branch"],
                "--title",
                run(root, "git", "log", "-1", "--format=%s"),
                "--body-file",
                str(body),
            )
            state["pr"] = pr_info(root)["number"]
        save(root, state)
    return {"sha": head, "push_target": f"origin/{state['branch']}", "pr": state["pr"]}


def feedback(root: Path, sha: str | None = None) -> dict[str, Any]:
    pr = pr_info(root)
    head = run(root, "git", "rev-parse", "HEAD")
    if sha and head != sha:
        msg = "Requested feedback SHA differs from the local feature head."
        raise RuntimeError(msg)
    checks = [
        item
        for item in (pr["statusCheckRollup"] or [])
        if item.get("name") in {"fast-checks", "agent-review", "agent-writer"}
    ]
    changes = [r for r in pr["reviews"] if (r.get("commit") or {}).get("oid") == pr["headRefOid"]]
    status = "merged" if pr["mergedAt"] else "closed" if pr["state"] == "CLOSED" else "pending"
    return {
        "status": status,
        "pr": pr["number"],
        "url": pr["url"],
        "sha": head,
        "remote_sha": pr["headRefOid"],
        "refresh_required": head != pr["headRefOid"],
        "checks": checks,
        "reviews": changes,
        "writer": "github" if any(x["name"] == "agent:writing" for x in pr["labels"]) else "local",
        "next": "Wait for merge; address current-head review and fast CI "
        "feedback. Refresh the feature branch before corrections after bot commits.",
    }


def refresh(root: Path) -> dict[str, Any]:
    writable_state = load(root)
    pr = pr_info(root)
    if any(x["name"] == "agent:writing" for x in pr["labels"]):
        msg = "GitHub agent is writing; wait before refreshing."
        raise RuntimeError(msg)
    clean(root)
    if run(root, "git", "branch", "--show-current") != writable_state["branch"]:
        msg = "Refresh requires the owned feature branch."
        raise RuntimeError(msg)
    run(root, "git", "pull", "--ff-only", "origin", writable_state["branch"])
    return {"status": "refreshed", "sha": run(root, "git", "rev-parse", "HEAD")}


def synchronize(root: Path) -> dict[str, Any]:
    state = load(root)
    if not pr_info(root)["mergedAt"]:
        msg = "Wait for the feature PR to merge before synchronizing."
        raise RuntimeError(msg)
    base = sync_dev(root)
    source = Path(state["source"])
    pending = None
    import fcntl  # noqa: PLC0415 - Unix-only launcher; MCP imports remain portable.

    lock_path = source / ".git" / "atk-sync.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            _sync_shared(source)
        except RuntimeError as exc:
            pending = str(exc)
    state.update(phase="planning", branch="dev", base=base)
    save(root, state)
    return {
        "status": "synchronized",
        "base": base,
        "shared_sync_pending": pending,
        "next": "Enter native Plan mode before planning another feature.",
    }


def _sync_shared(source: Path) -> None:
    if run(source, "git", "branch", "--show-current") != "dev":
        msg = "Shared checkout is not on dev."
        raise RuntimeError(msg)
    sync_dev(source)


def fragment(root: Path, summary: str, *, dry_run: bool = False) -> dict[str, Any]:
    state = load(root)
    if state.get("phase") != "feature":
        msg = "Changelog fragments require an active feature session."
        raise RuntimeError(msg)
    writable(root)
    path = f"agent_tools/changelog/{state['id']}.md"
    if not dry_run:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("- " + summary.strip().lstrip("- ") + "\n")
    return {"decision": "fragment", "path": path, "planned_version": None}


def repository_key(source: Path) -> str:
    return hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:12]
