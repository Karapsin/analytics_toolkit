# ruff: noqa: SLF001 - shares private release orchestration contracts.

"""Reconciled replacement generations for an explicitly requested release."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from release_routines.lib import release_candidate as candidate

from agent_tools import release_preparation as preparation
from agent_tools import sessions

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any


def reserved(root: Path, version: str) -> bool:
    """Propagate lookup failures rather than treating a version as available."""
    indexes = [candidate.index_files(stage, version) for stage in candidate.PROJECTS]
    tag = "v" + version
    remote = sessions.run(root, "git", "ls-remote", "--tags", "origin", tag, tag + "^{}")
    local = sessions.run(root, "git", "tag", "--list", tag)
    return bool(any(indexes) or remote or local)


def _version(
    root: Path,
    state: dict[str, Any],
    source: str,
    *,
    new_version: bool,
) -> str:
    mcp = preparation.mcp_runtime()
    current = str(
        mcp._parse_required(
            mcp.VERSION_RE,
            sessions.run(root, "git", "show", source + ":pyproject.toml"),
            "package version",
        )
    )
    previous = str(state["version"])
    higher = tuple(map(int, current.split("."))) > tuple(map(int, previous.split(".")))
    version = current if higher else previous
    if new_version or higher:
        version = str(mcp._increment_version(version))
    while reserved(root, version):
        version = str(mcp._increment_version(version))
    return version


def notes(work: Path, state: dict[str, Any], version: str) -> str:
    """Carry prepared notes into new notes, retaining every older version section."""
    mcp = preparation.mcp_runtime()
    previous = state["version"]
    text = (work / mcp.CHANGELOG_PATH).read_text()
    old = state.get("replacement_notes", state["files"])[mcp.CHANGELOG_PATH]
    pattern = r"(?m)^## " + re.escape(previous) + r" - [^\n]*\n[\s\S]*?(?=^## |\Z)"
    match = re.search(pattern, old)
    if not match:
        message = "Previous candidate has no versioned notes; replacement is blocked."
        raise RuntimeError(message)
    bullets = mcp._unreleased_changelog_bullets("## Unreleased\n" + match[0].split("\n", 1)[1])
    if version == previous:
        # Rewrite the same unpublished release into one section, retaining current
        # dev corrections as well as the predecessor's prepared notes.
        current = re.search(pattern, text)
        if current:
            bullets += mcp._unreleased_changelog_bullets(
                "## Unreleased\n" + current[0].split("\n", 1)[1],
            )
        text = re.sub(pattern, "", text)
    existing = set(mcp._unreleased_changelog_bullets(text))
    for bullet in bullets:
        if bullet not in existing:
            text = mcp._upsert_unreleased_changelog_bullet(text, bullet)
            existing.add(bullet)
    return text


def replace(
    root: Path,
    state: dict[str, Any],
    summary: str | None,
    *,
    force_version: bool = False,
) -> dict[str, Any]:
    """Journal one generation before publication; retry it without duplicating PRs."""
    new_version = candidate.reconcile_replacement(root, state, allow_reserved=True)
    intent = state.get("replacement_intent") if state.get("replacement_pending") else None
    if not intent:
        source = sessions.run(root, "git", "rev-parse", "origin/dev")
        intent = {
            "source_sha": source,
            "generation": state.get("generation", 0) + 1,
            "version": _version(root, state, source, new_version=new_version or force_version),
            "predecessor_sha": state["candidate_sha"],
            "summary": summary,
        }
        state["replacement_intent"] = intent
        state["replacement_pending"] = True
        preparation.save(root, state)
    # Evidence may change while recovering a journaled intent. Do not reuse a
    # newly reserved version even if the predecessor was unpublished initially.
    if reserved(root, intent["version"]):
        intent["version"] = _version(root, state, intent["source_sha"], new_version=True)
        preparation.save(root, state)
    archive = preparation.state_path(root).with_name(
        preparation.STATE + "." + state["candidate_sha"],
    )
    if not archive.exists():
        archive.write_text(json.dumps(state, indent=2) + "\n")
        archive.chmod(0o600)
    result = preparation._new(root, intent["summary"], predecessor=state)
    preparation._clear_receipts(root)
    result.pop("reset_receipts", None)
    preparation.save(root, result)
    return result
