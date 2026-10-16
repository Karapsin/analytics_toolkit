"""Independent local feature sessions; no public package dependencies."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

task_context = importlib.import_module(
    f"{__package__}.task_context" if __package__ else "task_context"
)

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
SSH_PUBLIC_PARTS = 2


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


def verify_signature(root: Path, head: str) -> None:
    key = load(root).get("signing_key")
    if not key or not Path(key).with_suffix(".pub").is_file():
        msg = "Published PR requires the local machine signing key."
        raise RuntimeError(msg)
    public = Path(key).with_suffix(".pub").read_text().strip().split()
    if len(public) < SSH_PUBLIC_PARTS or public[0] != "ssh-ed25519":
        msg = "Machine signing public key is invalid."
        raise RuntimeError(msg)
    allowed = state_path(root).with_name("atk-publication-signers")
    allowed.write_text("atk-local " + " ".join(public[:2]) + "\n")
    allowed.chmod(0o600)
    run(
        root,
        "git",
        "-c",
        "gpg.format=ssh",
        "-c",
        f"gpg.ssh.allowedSignersFile={allowed}",
        "verify-commit",
        head,
    )


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
    state.update(phase="feature", branch=branch, base=latest, feature_id=uuid.uuid4().hex)
    state.pop("pending_base", None)
    state.pop("pr", None)
    save(root, state)
    return {"status": "feature", "branch": branch, "base": latest}


def pr_info(root: Path, target: int | None = None) -> dict[str, Any]:
    state = load(root)
    target_ref = str(target or state.get("pr") or state["branch"])
    return json.loads(
        run(
            root,
            "gh",
            "pr",
            "view",
            target_ref,
            "--json",
            "number,url,state,mergedAt,headRefOid,headRefName,baseRefName,body,labels,reviews,statusCheckRollup",
        )
    )


def writable(root: Path) -> dict[str, Any]:
    state = load(root)
    branch = run(root, "git", "branch", "--show-current")
    if state.get("phase") != "feature" or branch != state.get("branch"):
        msg = "Feature writes require the session-owned feature branch."
        raise RuntimeError(msg)
    if any(item["branch"] == branch for item in state.get("handoffs", [])):
        msg = "Published PR ownership belongs to the Pi; local corrections are disabled."
        raise RuntimeError(msg)
    if state.get("pr"):
        pr = pr_info(root)
        if pr["state"] != "OPEN":
            msg = "PR is closed; synchronize before starting another feature."
            raise RuntimeError(msg)
        if any(label["name"] == "agent:writing" for label in pr["labels"]):
            msg = "GitHub agent owns this branch; wait for feedback before editing."
            raise RuntimeError(msg)
        if any(label["name"] == "agent:needs-user" for label in pr["labels"]):
            raise RuntimeError(
                "PR is awaiting user instructions; do not make automatic corrections. "
                "Owner must post /agent resume " + pr["headRefOid"]
            )
        remote = pr["headRefOid"]
        run(root, "git", "fetch", "origin", state["branch"])
        if run(root, "git", "merge-base", "--is-ancestor", remote, "HEAD"):
            msg = "Unexpected ancestry result."
            raise RuntimeError(msg)
    return state


def push(root: Path) -> dict[str, Any]:
    state = writable(root)
    clean(root)
    prefix = (
        "Implemented in an isolated Codex session. The Pi owns independent "
        "review, corrections, GitHub checks and merging."
    )
    # Complete transport preflight precedes even the feature push. Archive refs
    # contain only task data and do not change the reviewed candidate tree.
    task_context.protocol().publication(task_context.read(root), prefix=prefix)
    head = run(root, "git", "rev-parse", "HEAD")
    task_context.protocol().verify_commit(
        task_context.protocol().public_context(task_context.read(root)),
        run(root, "git", "log", "-1", "--format=%B"),
    )
    body_text = task_context.publish_attachment(root, prefix=prefix)
    # Archive I/O can outlive a new hold or ownership change. Recheck the exact
    # session/head and signed attachment before publishing the candidate.
    if writable(root) != state or run(root, "git", "rev-parse", "HEAD") != head:
        message = "Session or candidate changed during task archive publication."
        raise RuntimeError(message)
    clean(root)
    task_context.protocol().verify_commit(
        task_context.parse_attachment(root, body_text),
        run(root, "git", "log", "-1", "--format=%B"),
    )
    task_context.protocol().verify_commit(
        task_context.protocol().public_context(task_context.read(root)),
        run(root, "git", "log", "-1", "--format=%B"),
    )
    run(root, "git", "push", "--set-upstream", "origin", f"HEAD:{state['branch']}")
    state["publication_sha"] = head
    save(root, state)
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
            body.write_text(body_text)
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
    body = state_path(root).with_name("atk-pr-body.md")
    body.write_text(body_text)
    run(root, "gh", "pr", "edit", str(state["pr"]), "--body-file", str(body))
    receipt = handoff(root)
    return {"sha": head, "push_target": f"origin/{state['branch']}", "pr": state["pr"], **receipt}


def handoff(root: Path) -> dict[str, Any]:
    """Verify publication, durably relinquish ownership, then refresh clean dev."""
    state = load(root)
    if state.get("phase") == "handed_off":
        return _finish_handoff(root, state)
    if state.get("phase") == "planning" and state.get("handoffs"):
        return {"status": "handed_off", **state["handoffs"][-1]}
    if state.get("phase") != "feature" or not state.get("pr"):
        msg = "Handoff requires a published feature PR."
        raise RuntimeError(msg)
    clean(root)
    if run(root, "git", "branch", "--show-current") != state["branch"]:
        msg = "Handoff requires the session-owned feature branch."
        raise RuntimeError(msg)
    head = state.get("publication_sha") or run(root, "git", "rev-parse", "HEAD")
    verify_signature(root, head)
    context = task_context.read(root)
    proto = task_context.protocol()
    proto.verify_commit(
        proto.public_context(context), run(root, "git", "log", "-1", "--format=%B", head)
    )
    pr = pr_info(root)
    if pr["baseRefName"] != "dev" or pr["headRefName"] != state["branch"]:
        msg = "Published PR branch/base does not match the owned feature."
        raise RuntimeError(msg)
    if pr["state"] != "OPEN" and not pr["mergedAt"]:
        msg = "Published PR is closed without a merge; retain local ownership."
        raise RuntimeError(msg)
    attached = task_context.parse_attachment(root, pr["body"])
    if proto.digest(attached) != proto.digest(proto.public_context(context)):
        msg = "Published PR task attachment does not match the signed local task."
        raise RuntimeError(msg)
    if pr["headRefOid"] != head:
        run(root, "git", "fetch", "origin", pr["headRefOid"])
        run(root, "git", "merge-base", "--is-ancestor", head, pr["headRefOid"])
    receipt = {
        "pr": pr["number"],
        "url": pr["url"],
        "branch": state["branch"],
        "sha": head,
        "task_id": context["task_id"],
        "task_digest": proto.digest(proto.public_context(context)),
        "owner": "pi",
    }
    state.setdefault("handoffs", []).append(receipt)
    state.update(phase="handed_off", publication_sha=head)
    save(root, state)  # Persist ownership before archiving or any network synchronization.
    return _finish_handoff(root, state)


def _finish_handoff(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    receipt = dict(state["handoffs"][-1])
    try:
        _archive_handoff_task(root, state)
        sync = synchronize(root)
    except (RuntimeError, OSError) as exc:
        sync = {"local_sync_pending": str(exc)}
    return {
        **receipt,
        **sync,
        "status": "handed_off",
        "next": "The Pi owns this PR. Refresh dev if pending, then enter native Plan mode "
        "for another task.",
    }


def _archive_handoff_task(root: Path, state: dict[str, Any]) -> None:
    if (task_context.directory(root) / "active.json").exists():
        active = task_context.read(root)
        if active["task_id"] != state["handoffs"][-1]["task_id"]:
            msg = "Pending handoff cannot archive a different active task; preserve its prompt."
            raise RuntimeError(msg)
        task_context.finish(root)


def feedback(root: Path, sha: str | None = None, target: int | None = None) -> dict[str, Any]:
    state = load(root)
    if target is None and not state.get("pr") and state.get("handoffs"):
        target = state["handoffs"][-1]["pr"]
    pr = pr_info(root, target)
    head = run(root, "git", "rev-parse", "HEAD")
    if sha and pr["headRefOid"] != sha:
        msg = "Requested feedback SHA differs from the current PR head."
        raise RuntimeError(msg)
    checks = [
        item
        for item in (pr["statusCheckRollup"] or [])
        if item.get("name") in {"fast-checks", "agent-review", "agent-writer"}
    ]
    changes = [r for r in pr["reviews"] if (r.get("commit") or {}).get("oid") == pr["headRefOid"]]
    status = "merged" if pr["mergedAt"] else "closed" if pr["state"] == "CLOSED" else "pending"
    paused = any(x["name"] == "agent:needs-user" for x in pr["labels"])
    if paused and status == "pending":
        status = "awaiting_user"
    return {
        "status": status,
        "pr": pr["number"],
        "url": pr["url"],
        "sha": head,
        "remote_sha": pr["headRefOid"],
        "refresh_required": state.get("phase") == "feature"
        and target is None
        and head != pr["headRefOid"],
        "checks": checks,
        "reviews": changes,
        "writer": "github",
        "next": "Status only. The Pi owns corrections, checks and merging; "
        "local agents need not wait.",
    }


def refresh(root: Path) -> dict[str, Any]:
    writable_state = load(root)
    if writable_state.get("phase") != "feature":
        msg = "Published PR ownership belongs to the Pi; refresh local dev instead."
        raise RuntimeError(msg)
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
    if state.get("phase") not in {"handed_off", "planning", "revalidate"}:
        msg = "Publish and hand off the PR before synchronizing."
        raise RuntimeError(msg)
    # The refreshed dev may predate this workflow addition. Load runtime helpers
    # before switching away from the feature's files, retaining their code in memory.
    maintenance_tools = importlib.import_module(
        f"{__package__}.session_cleanup" if __package__ else "session_cleanup"
    )
    if state.get("phase") == "handed_off":
        _archive_handoff_task(root, state)
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
    state.pop("pr", None)
    state.pop("publication_sha", None)
    state.pop("feature_id", None)
    save(root, state)
    maintenance = maintenance_tools.cleanup(root)
    return {
        "status": "synchronized",
        "base": base,
        "shared_sync_pending": pending,
        "branch_cleanup": maintenance,
        "next": "Enter native Plan mode before planning another feature.",
    }


def _sync_shared(source: Path) -> None:
    if run(source, "git", "branch", "--show-current") != "dev":
        msg = "Shared checkout is not on dev."
        raise RuntimeError(msg)
    sync_dev(source)


def fragment_path(state: dict[str, Any]) -> str:
    # Existing in-flight sessions retain their already committed fragment name.
    return f"agent_tools/changelog/{state.get('feature_id') or state['id']}.md"


def fragment(root: Path, summary: str, *, dry_run: bool = False) -> dict[str, Any]:
    state = load(root)
    if state.get("phase") != "feature":
        msg = "Changelog fragments require an active feature session."
        raise RuntimeError(msg)
    if not dry_run:
        writable(root)
    path = fragment_path(state)
    if not dry_run:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("- " + summary.strip().lstrip("- ") + "\n")
    return {"decision": "fragment", "path": path, "planned_version": None}


def repository_key(source: Path) -> str:
    return hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:12]
