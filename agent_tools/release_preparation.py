# ruff: noqa: SLF001 - release preparation shares repository-private metadata contracts.

"""Reviewed release candidates and private, resumable release orchestration.

Only explicit release actions call prepare. Status and upload guards call verify.
"""

from __future__ import annotations

import importlib
import json
import re
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_tools import sessions, task_context

if TYPE_CHECKING:
    from collections.abc import Iterator

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


@contextmanager
def release_lock(root: Path) -> Iterator[None]:
    """Serialize release-state mutations; process exit releases the OS lock."""
    import fcntl  # noqa: PLC0415 - release orchestration runs on the Unix host.

    with state_path(root).with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            message = "Another release action owns this candidate; retry after it finishes."
            raise RuntimeError(message) from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _tree(root: Path, revision: str) -> str:
    return sessions.run(root, "git", "rev-parse", revision + "^{tree}")


def _pr(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    target = str(state.get("pr") or state["branch"])
    pr: dict[str, Any] = json.loads(
        sessions.run(
            root,
            "gh",
            "pr",
            "view",
            target,
            "--json",
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


def _metadata_files(
    work: Path,
    summary: str | None,
    *,
    version: str | None = None,
    changelog: str | None = None,
) -> tuple[str, dict[str, str], list[str]]:
    mcp = mcp_runtime()
    if changelog is None:
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
    notes = list(dict.fromkeys(notes))
    if not notes:
        message = "No unreleased entries; supply --summary for this release."
        raise ValueError(message)
    version = version or mcp._increment_version(mcp._package_version(work))
    files = {
        "pyproject.toml": mcp._replace_required(
            mcp.VERSION_RE,
            (work / "pyproject.toml").read_text(),
            f'version = "{version}"',
            "project version",
        ),
        "README.md": mcp._replace_required(
            mcp.README_VERSION_RE,
            (work / "README.md").read_text(),
            f"**Version:** `{version}`",
            "README version",
        ),
        mcp.CHANGELOG_PATH: mcp._release_unreleased_changelog_text(
            changelog,
            mcp._format_changelog_entry(version, notes),
        ),
    }
    return version, files, [str(path.relative_to(work)) for path in fragments]


def _new(
    root: Path,
    summary: str | None,
    *,
    predecessor: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    generation = 0
    if predecessor:
        from agent_tools import release_replacement  # noqa: PLC0415

        intent = predecessor["replacement_intent"]
        sessions.run(work, "git", "fetch", "origin", intent["source_sha"])
        sessions.run(work, "git", "switch", "--detach", intent["source_sha"])
        source = intent["source_sha"]
        generation = intent["generation"]
        changelog = release_replacement.notes(work, predecessor, intent["version"])
        version, files, fragments = _metadata_files(
            work,
            summary,
            version=intent["version"],
            changelog=changelog,
        )
    else:
        version, files, fragments = _metadata_files(work, summary)
    branch = f"codex/release-{context['task_id']}-{source[:12]}"
    if generation:
        branch += f"-g{generation}"
    receipt = sessions.load(work)
    receipt.update(
        phase="feature",
        branch=branch,
        base=source,
        signing_key=key,
        release_preparation=True,
        feature_id=context["task_id"],
    )
    sessions.save(work, receipt)
    task_dir = task_context.directory(work)
    shutil.copyfile(task_context.directory(root) / "active.json", task_dir / "active.json")
    (task_dir / "active.json").chmod(0o600)
    state = {
        "task_id": context["task_id"],
        "task_digest": proto.digest(proto.public_context(context)),
        "source_sha": source,
        "version": version,
        "branch": branch,
        "workspace": str(work),
        "files": files,
        "fragments": fragments,
        "candidate_sha": None,
        "pr": None,
        "stages": {},
        "generation": generation,
    }
    if predecessor:
        state.update(
            predecessor_sha=predecessor["candidate_sha"],
            replacement_intent=predecessor["replacement_intent"],
            reset_receipts=True,
        )
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
                work,
                "git",
                *sessions.commit_options(work),
                "commit",
                "-m",
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


def _reviewed_metadata(root: Path, state: dict[str, Any], sha: str) -> dict[str, str]:
    mcp = mcp_runtime()
    files = {path: sessions.run(root, "git", "show", f"{sha}:{path}") + "\n" for path in METADATA}
    for pattern, path, label in (
        (mcp.VERSION_RE, "pyproject.toml", "package version"),
        (mcp.README_VERSION_RE, "README.md", "README version"),
    ):
        if mcp._parse_required(pattern, files[path], label) != state["version"]:
            message = "Corrected release metadata must preserve the prepared version."
            raise RuntimeError(message)
    changelog = files["docs/CHANGELOG.md"]
    if mcp._unreleased_changelog_bullets(changelog):
        message = "Corrected release preparation left unreleased entries."
        raise RuntimeError(message)
    sections = re.split(r"(?m)^## ", changelog)
    notes = [section for section in sections if section.startswith(state["version"] + " - ")]
    if len(notes) != 1 or not any(line.startswith("- ") for line in notes[0].splitlines()):
        message = "Corrected preparation must retain versioned release notes."
        raise RuntimeError(message)
    fragments = sessions.run(
        root, "git", "ls-tree", "-r", "--name-only", sha, "agent_tools/changelog"
    )
    if any(path.endswith(".md") for path in fragments.splitlines()):
        message = "Corrected release preparation left unconsumed changelog fragments."
        raise RuntimeError(message)
    return files


def _clear_receipts(root: Path) -> None:
    mcp = mcp_runtime()
    for receipt in (
        mcp.CHECK_STATE_FILE,
        mcp.PRECOMMIT_STAGE_STATE_FILE,
        mcp.RELEASE_CHECK_STATE_FILE,
    ):
        path = root / receipt
        if path.exists():
            path.unlink()


def _adopt_reviewed_head(root: Path, state: dict[str, Any], sha: str, tree: str) -> None:
    files = _reviewed_metadata(root, state, sha)
    candidate = importlib.import_module("release_routines.lib.release_candidate")
    candidate.reconcile_replacement(root, state)
    archived = state_path(root).with_name(STATE + "." + state["candidate_sha"])
    if not archived.exists():
        archived.write_text(json.dumps(state, indent=2) + "\n")
        archived.chmod(0o600)
    # Exact-tree checks cannot be reused for corrected content.
    _clear_receipts(root)
    for key in list(state):
        if key.startswith(("testpypi_", "pypi_")) or key == "requires_new_version":
            state.pop(key)
    state.update(candidate_sha=sha, candidate_tree=tree, files=files, stages={})
    save(root, state)


def prepare(root: Path, *, summary: str | None = None) -> dict[str, Any]:
    with release_lock(root):
        return _prepare(root, summary=summary)


def _prepare(  # noqa: C901, PLR0912 - journaled release generations.
    root: Path,
    *,
    summary: str | None = None,
) -> dict[str, Any]:
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
    if state and state.get("replacement_pending"):
        from agent_tools import release_replacement  # noqa: PLC0415

        state = release_replacement.replace(root, state, summary)
    elif state and state.get("requires_new_version"):
        from agent_tools import release_replacement  # noqa: PLC0415

        state = release_replacement.replace(root, state, summary, force_version=True)
    elif not state:
        state = _new(root, summary)
    if state.get("reset_receipts"):
        _clear_receipts(root)
        state.pop("reset_receipts")
        save(root, state)
    if not state.get("pr"):
        _publish_preparation(root, state)
    pr = _pr(root, state)
    _context(state, pr)
    if not pr.get("mergedAt"):
        if pr["state"] == "CLOSED":
            message = "Release preparation PR was closed without merging."
            raise RuntimeError(message)
        return {
            "status": "pending",
            "pr": pr["number"],
            "url": pr["url"],
            "version": state["version"],
            "candidate_sha": state["candidate_sha"],
        }
    final_sha = pr["headRefOid"]
    final_state = {**state, "candidate_sha": final_sha}
    _reviewed(root, final_state)
    sessions.run(root, "git", "fetch", "origin", final_sha)
    final_tree = _tree(root, final_sha)
    if _tree(root, "origin/dev") != final_tree:
        state["replacement_notes"] = _reviewed_metadata(root, final_state, final_sha)
        from agent_tools import release_replacement  # noqa: PLC0415

        release_replacement.replace(root, state, summary)
        return _prepare(root, summary=None)
    if final_sha != state["candidate_sha"]:
        _adopt_reviewed_head(root, state, final_sha, final_tree)
    elif final_tree != state["candidate_tree"]:
        message = "Recorded candidate tree differs from the reviewed head."
        raise RuntimeError(message)
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
        if _tree(root, "HEAD") != tree or sessions.run(
            root, "git", "rev-parse", "HEAD"
        ) != sessions.run(root, "git", "rev-parse", "origin/main"):
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
