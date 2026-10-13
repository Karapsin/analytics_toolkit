"""Reviewed release candidates and private, resumable release orchestration.

Only explicit release actions call prepare. Status and upload guards call verify.
"""
from __future__ import annotations

# ruff: noqa: SLF001 - release preparation shares repository-private metadata contracts.

import importlib
import json
import shutil
from pathlib import Path
from typing import Any

from agent_tools import sessions, task_context

STATE = "atk-release.json"
METADATA = ("pyproject.toml", "README.md", "docs/CHANGELOG.md")


def mcp_runtime() -> Any:
    return importlib.import_module("agent_tools.mcp_server")


def state_path(root: Path) -> Path:
    return Path(sessions.run(root, "git", "rev-parse", "--absolute-git-dir")) / STATE


def load(root: Path) -> dict[str, Any]:
    path = state_path(root)
    state: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
    return state


def save(root: Path, state: dict[str, Any]) -> None:
    path = state_path(root)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def _tree(root: Path, revision: str) -> str:
    return sessions.run(root, "git", "rev-parse", revision + "^{tree}")


def _pr(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    target = str(state.get("pr") or state["branch"])
    pr: dict[str, Any] = json.loads(
        sessions.run(
            root, "gh", "pr", "view", target, "--json",
            "number,url,state,mergedAt,headRefOid,headRefName,baseRefName,body,statusCheckRollup",
        )
    )
    return pr


def _context(state: dict[str, Any], pr: dict[str, Any]) -> None:
    proto = task_context.protocol()
    attached = proto.parse(pr["body"])
    if (
        attached["task_id"] != state["task_id"]
        or proto.digest(attached) != state["task_digest"]
        or pr["headRefName"] != state["branch"]
        or pr["baseRefName"] != "dev"
    ):
        message = "Release PR does not match its signed release task and branch."
        raise RuntimeError(message)


def _reviewed(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    pr = _pr(root, state)
    _context(state, pr)
    if not pr.get("mergedAt"):
        message = "Release preparation PR must be independently reviewed and merged."
        raise RuntimeError(message)
    if pr["headRefOid"] != state["candidate_sha"]:
        message = "Release PR content changed; prepare and review a new candidate."
        raise RuntimeError(message)
    # The merged PR is subject to controller provenance and exact-head policy.
    # Reconfirm exact-head required checks rather than trusting a local stage flag.
    checks = pr.get("statusCheckRollup") or []
    for name in ("fast-checks", "agent-review"):
        matching = [item for item in checks if item.get("name") == name]
        if not matching or any(item.get("conclusion") != "SUCCESS" for item in matching):
            message = "Prepared candidate requires successful " + name + "."
            raise RuntimeError(message)
    return pr


def _metadata_files(work: Path, summary: str | None) -> tuple[str, dict[str, str], list[str]]:
    mcp = mcp_runtime()
    changelog = (work / mcp.CHANGELOG_PATH).read_text()
    fragments = sorted((work / "agent_tools/changelog").glob("*.md"))
    for fragment in fragments:
        bullet = fragment.read_text().strip()
        if not bullet.startswith("- ") or "\n" in bullet:
            message = "Changelog fragments must contain one concise bullet."
            raise ValueError(message)
        changelog = mcp._upsert_unreleased_changelog_bullet(changelog, bullet)
    notes = mcp._unreleased_changelog_bullets(changelog)
    if summary and summary.strip():
        notes.append(mcp._format_changelog_bullet(summary))
    if not notes:
        message = "No unreleased entries; supply --summary for this release."
        raise ValueError(message)
    version = mcp._increment_version(mcp._package_version(work))
    files = {
        "pyproject.toml": mcp._replace_required(
            mcp.VERSION_RE, (work / "pyproject.toml").read_text(),
            f'version = "{version}"', "project version",
        ),
        "README.md": mcp._replace_required(
            mcp.README_VERSION_RE, (work / "README.md").read_text(),
            f"**Version:** `{version}`", "README version",
        ),
        mcp.CHANGELOG_PATH: mcp._release_unreleased_changelog_text(
            changelog, mcp._format_changelog_entry(version, notes),
        ),
    }
    return version, files, [str(path.relative_to(work)) for path in fragments]


def _new(root: Path, summary: str | None) -> dict[str, Any]:
    context = task_context.read(root)  # Require the actual explicit release request.
    proto = task_context.protocol()
    key = sessions.load(root).get("signing_key")
    if not key:
        key = str(Path.home() / ".local/share/analytics-toolkit-agent/identity/signing_ed25519")
    if not Path(key).is_file() or not Path(key).with_suffix(".pub").is_file():
        message = "Release preparation requires an existing enrolled machine signing key."
        raise RuntimeError(message)
    work = sessions.create(root, state_path(root).parent / "atk-release-workspaces")
    source = sessions.run(work, "git", "rev-parse", "HEAD")
    version, files, fragments = _metadata_files(work, summary)
    branch = f"codex/release-{context['task_id']}-{source[:12]}"
    receipt = sessions.load(work)
    receipt.update(
        phase="feature", branch=branch, base=source, signing_key=key,
        release_preparation=True, feature_id=context["task_id"],
    )
    sessions.save(work, receipt)
    task_dir = task_context.directory(work)
    shutil.copyfile(task_context.directory(root) / "active.json", task_dir / "active.json")
    (task_dir / "active.json").chmod(0o600)
    state = {
        "task_id": context["task_id"], "task_digest": proto.digest(proto.public_context(context)),
        "source_sha": source, "version": version, "branch": branch,
        "workspace": str(work), "files": files, "fragments": fragments,
        "candidate_sha": None, "pr": None, "stages": {},
    }
    save(root, state)  # Journal the intended replacements before writing any metadata.
    return state


def _publish_preparation(root: Path, state: dict[str, Any]) -> None:
    work = Path(state["workspace"])
    if not state["candidate_sha"]:
        head = sessions.run(work, "git", "rev-parse", "HEAD")
        if head != state["source_sha"]:
            # Recover a signed commit made just before the journal update.
            sessions.verify_signature(work, head)
            task_context.protocol().verify_commit(
                task_context.protocol().public_context(task_context.read(work)),
                sessions.run(work, "git", "log", "-1", "--format=%B"),
            )
            for path, content in state["files"].items():
                if sessions.run(work, "git", "show", f"HEAD:{path}") != content.strip():
                    message = "Interrupted preparation has unexpected committed content."
                    raise RuntimeError(message)
        else:
            branch = sessions.run(work, "git", "branch", "--show-current")
            if branch != state["branch"]:
                sessions.run(work, "git", "switch", "-c", state["branch"])
            for path, content in state["files"].items():
                (work / path).write_text(content)
            for path in state["fragments"]:
                if (work / path).exists():
                    (work / path).unlink()
            sessions.run(work, "git", "add", "--", *METADATA, *state["fragments"])
            sessions.run(
                work, "git", *sessions.commit_options(work), "commit", "-m",
                "Prepare PyPI release " + state["version"] + task_context.commit_trailers(work),
            )
            head = sessions.run(work, "git", "rev-parse", "HEAD")
        state["candidate_sha"] = head
        state["candidate_tree"] = _tree(work, head)
        save(root, state)
    # Existing publication mechanisms verify signatures and task attachments and
    # recover interrupted PR creation. Once handed off, only read the existing PR.
    session = sessions.load(work)
    if session.get("phase") == "feature":
        sessions.push(work)
    pr = _pr(root, state)
    _context(state, pr)
    state.update(pr=pr["number"], pr_url=pr["url"])
    save(root, state)


def prepare(root: Path, *, summary: str | None = None) -> dict[str, Any]:
    sessions.clean(root)
    sessions.run(root, "git", "fetch", "origin", "dev", "main")
    state = load(root)
    active_path = task_context.directory(root) / "active.json"
    if state and active_path.exists():
        active = task_context.read(root)
        if active["task_id"] != state["task_id"]:
            if state["stages"].get("pypi_verified") != state["candidate_sha"]:
                message = "Resume the existing release task before preparing a different release."
                raise RuntimeError(message)
            state = _new(root, summary)
    if not state or state.get("requires_new_version"):
        state = _new(root, summary)
    if not state.get("pr"):
        _publish_preparation(root, state)
    pr = _pr(root, state)
    _context(state, pr)
    if not pr.get("mergedAt"):
        if pr["state"] == "CLOSED":
            message = "Release preparation PR was closed without merging."
            raise RuntimeError(message)
        return {
            "status": "pending", "pr": pr["number"], "url": pr["url"],
            "version": state["version"], "candidate_sha": state["candidate_sha"],
        }
    sessions.run(root, "git", "fetch", "origin", state["candidate_sha"])
    if (
        pr["headRefOid"] != state["candidate_sha"]
        or _tree(root, "origin/dev") != state["candidate_tree"]
    ):
        # Changed content needs a new reviewed preparation. The old candidate and
        # its immutable artifacts stay available in an archival receipt.
        archived = state_path(root).with_name(STATE + "." + state["candidate_sha"])
        archived.write_text(json.dumps(state, indent=2) + "\n")
        archived.chmod(0o600)
        state = _new(root, summary)
        _publish_preparation(root, state)
        return {
            "status": "pending", "pr": state["pr"], "url": state["pr_url"],
            "version": state["version"], "candidate_sha": state["candidate_sha"],
        }
    _reviewed(root, state)
    state["stages"]["reviewed"] = state["candidate_sha"]
    save(root, state)
    return {
        "status": "prepared",
        "pr": pr["number"],
        "url": pr["url"],
        "version": state["version"],
    }


def verify(root: Path, *, promotion: bool = False) -> dict[str, Any]:
    state = load(root)
    if not state or not state.get("candidate_sha") or not state.get("pr"):
        message = "Missing reviewed release preparation; run release-workflow --action merge-dev."
        raise RuntimeError(message)
    sessions.clean(root)
    sessions.run(root, "git", "fetch", "origin", "dev", "main", state["candidate_sha"])
    _reviewed(root, state)
    tree = _tree(root, state["candidate_sha"])
    if tree != state.get("candidate_tree") or _tree(root, "origin/dev") != tree:
        message = "Current dev content differs from the reviewed release candidate."
        raise RuntimeError(message)
    if not promotion:
        if sessions.run(root, "git", "branch", "--show-current") != "main":
            message = "Release uploads require production main."
            raise RuntimeError(message)
        if _tree(root, "HEAD") != tree or sessions.run(root, "git", "rev-parse", "HEAD") != sessions.run(
            root, "git", "rev-parse", "origin/main"
        ):
            message = "Production main must be synchronized with the prepared candidate."
            raise RuntimeError(message)
        mcp = mcp_runtime()
        if mcp._package_version(root) != state["version"]:
            message = "Prepared version does not match package metadata."
            raise RuntimeError(message)
        if mcp._unreleased_changelog_bullets((root / mcp.CHANGELOG_PATH).read_text()):
            message = "Release preparation left unreleased entries."
            raise RuntimeError(message)
    return {**state, "promotion_sha": sessions.run(root, "git", "rev-parse", "origin/dev")}
