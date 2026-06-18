#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import docs_assistant

try:  # pragma: no cover - exercised only when the agent-only MCP dependency exists.
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - normal package test env has no MCP dependency.
    FastMCP = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from release_routines.lib.check_readme_dependencies import validate_readme_dependencies
from release_routines.lib.project_metadata import load_project

DEFAULT_INDEX_DIR = docs_assistant.DEFAULT_INDEX_DIR
CHECK_STATE_FILE = Path(DEFAULT_INDEX_DIR) / "precommit_check.json"
RELEASE_CHECK_STATE_FILE = Path(DEFAULT_INDEX_DIR) / "release_check.json"
SENSITIVE_LOCAL_PATHS = {
    ".connections",
    ".env",
}
SENSITIVE_LOCAL_DIRS = {
    ".certs",
    ".connections",
    ".env",
    ".mypy_cache",
    ".pytest_cache",
    ".rag_index",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
}
REQUIRED_VERSION_PATHS = {
    "pyproject.toml",
    "README.md",
    "docs/CHANGELOG.md",
}
CHANGELOG_PATH = "docs/CHANGELOG.md"
UNRELEASED_CHANGELOG_THRESHOLD = 10
WORK_BRANCH = "dev"
RELEASE_BRANCH = "main"
VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', flags=re.MULTILINE)
PYTHON_REQUIRES_RE = re.compile(r'^requires-python\s*=\s*"([^"]+)"', flags=re.MULTILINE)
README_VERSION_RE = re.compile(r"\*\*Version:\*\*\s+`([^`]+)`")
CHANGELOG_HEADING_RE = re.compile(r"^##\s+([0-9]+(?:\.[0-9]+){3})\s+-\s+(.+?)\s*$", flags=re.MULTILINE)
UNRELEASED_HEADING_RE = re.compile(r"^##\s+Unreleased\s*$", flags=re.IGNORECASE | re.MULTILINE)
DEPENDENCY_RE = re.compile(r'"([^"]+)"')

MODULE_DOCS = {
    "ab_utils": "agent_docs/ab_utils.md",
    "ab": "agent_docs/ab_utils.md",
    "agent_tool": "agent_tools/README.md",
    "agent_tools": "agent_tools/README.md",
    "sql": "agent_docs/sql.md",
    "excel": "agent_docs/excel.md",
    "dates": "agent_docs/dates.md",
    "date": "agent_docs/dates.md",
    "general": "agent_docs/general.md",
    "docs_assistant": "agent_tools/README.md",
    "mcp": "agent_tools/README.md",
    "rag": "agent_tools/README.md",
}

TASK_DOCS = {
    "development": "agent_docs/development.md",
    "implement": "agent_docs/development.md",
    "implementation": "agent_docs/development.md",
    "change": "agent_docs/development.md",
    "testing": "agent_docs/development.md",
    "test": "agent_docs/development.md",
    "build": "agent_docs/development.md",
    "commit": "agent_docs/development.md",
    "documentation": "agent_docs/documentation.md",
    "docs": "agent_docs/documentation.md",
    "readme": "agent_docs/documentation.md",
    "release": "agent_docs/release.md",
    "pypi": "agent_docs/release.md",
    "publish": "agent_docs/release.md",
    "instruction": "AGENTS.md",
    "instructions": "AGENTS.md",
}

TEST_COMMANDS = {
    "ab_utils": [
        {
            "display": "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_ab_utils_metrics.py",
            "args": ["pytest", "-q", "tests/test_ab_utils_metrics.py"],
            "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
        },
        {
            "display": "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_ab_utils_format.py tests/test_ab_utils_split.py tests/test_ab_utils_parallel.py",
            "args": [
                "pytest",
                "-q",
                "tests/test_ab_utils_format.py",
                "tests/test_ab_utils_split.py",
                "tests/test_ab_utils_parallel.py",
            ],
            "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
        },
    ],
    "dates": [
        {
            "display": "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_dates.py",
            "args": ["pytest", "-q", "tests/test_dates.py"],
            "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
        },
    ],
    "excel": [
        {
            "display": "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_excel_long_format.py",
            "args": ["pytest", "-q", "tests/test_excel_long_format.py"],
            "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
        },
    ],
    "general": [
        {
            "display": "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_general_read_file.py tests/test_general_logging.py",
            "args": [
                "pytest",
                "-q",
                "tests/test_general_read_file.py",
                "tests/test_general_logging.py",
            ],
            "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
        },
    ],
    "sql": [
        {
            "display": "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_sql_connection_config.py tests/test_sql_retries.py tests/test_sql_load_table.py",
            "args": [
                "pytest",
                "-q",
                "tests/test_sql_connection_config.py",
                "tests/test_sql_retries.py",
                "tests/test_sql_load_table.py",
            ],
            "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
        },
    ],
    "agent_tools": [
        {
            "display": "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_rag_docs.py tests/test_agent_tools_mcp.py",
            "args": ["pytest", "-q", "tests/test_rag_docs.py", "tests/test_agent_tools_mcp.py"],
            "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
        },
    ],
    "mcp": [
        {
            "display": "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_agent_tools_mcp.py",
            "args": ["pytest", "-q", "tests/test_agent_tools_mcp.py"],
            "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
        },
    ],
}

PRECOMMIT_COMMAND = {
    "display": "release_routines/pre_commit_checks.sh",
    "args": ["release_routines/pre_commit_checks.sh"],
    "env": {},
}

RELEASE_CHECK_COMMANDS = [
    {
        "display": "release_routines/scripts/check_package_metadata.sh",
        "args": ["release_routines/scripts/check_package_metadata.sh"],
        "env": {},
    },
    {
        "display": "release_routines/scripts/check_readme_dependencies.sh",
        "args": ["release_routines/scripts/check_readme_dependencies.sh"],
        "env": {},
    },
    {
        "display": "release_routines/scripts/check_docs_links.sh",
        "args": ["release_routines/scripts/check_docs_links.sh"],
        "env": {},
    },
    {
        "display": "release_routines/scripts/check_docs_coverage.sh",
        "args": ["release_routines/scripts/check_docs_coverage.sh"],
        "env": {},
    },
]


class _FingerprintError(RuntimeError):
    def __init__(self, phase: str, result: dict[str, Any]) -> None:
        self.phase = phase
        self.result = result
        summary = (
            result.get("summary")
            or result.get("stderr")
            or result.get("stdout")
            or "git command failed"
        )
        super().__init__(f"Could not fingerprint working tree during {phase}: {summary}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "command": self.result.get("command"),
            "returncode": self.result.get("returncode"),
            "stderr": str(self.result.get("stderr", "")).strip(),
            "stdout": str(self.result.get("stdout", "")).strip(),
            "message": str(self),
        }


def prepare_start(
    task: str,
    module: str | None = None,
    root: str = ".",
    index_dir: str = DEFAULT_INDEX_DIR,
    ensure_project_env: bool = True,
) -> dict[str, Any]:
    """Run the mandatory startup workflow for coding agents."""
    root_path = _resolve_root(root)
    input_summary = {
        "task": task,
        "module": module,
        "root": str(root_path),
        "index_dir": index_dir,
        "ensure_project_env": ensure_project_env,
    }
    command_results: list[dict[str, Any]] = []

    for phase, command in _prepare_start_commands(root_path, ensure_project_env):
        result = _run_command(root_path, command)
        command_results.append(result)
        if not result["ok"]:
            return _tool_output(
                "prepare_start",
                input_summary,
                ok=False,
                summary=f"{phase} failed; stop and report the blocker.",
                result={"phase": phase},
                command_results=command_results,
                blockers=[_command_blocker(phase, result)],
                next_actions=["Resolve the startup blocker, then rerun prepare_start."],
            )

    health = repo_health(root=str(root_path))
    if health["branch"] != WORK_BRANCH:
        return _tool_output(
            "prepare_start",
            input_summary,
            ok=False,
            summary=f"startup branch verification failed; expected {WORK_BRANCH}.",
            result={"phase": "branch_verify", "repo_health": health},
            command_results=command_results,
            blockers=[
                {
                    "phase": "branch_verify",
                    "message": f"prepare_start must leave the repository on {WORK_BRANCH}.",
                    "branch": health["branch"],
                    "expected_branch": WORK_BRANCH,
                }
            ],
            next_actions=[f"Switch to {WORK_BRANCH}, resolve checkout issues, then rerun prepare_start."],
        )

    try:
        index = docs_assistant.build_docs_index(root=root_path, index_dir=index_dir)
    except Exception as exc:
        return _tool_output(
            "prepare_start",
            input_summary,
            ok=False,
            summary="docs index rebuild failed; stop and report the blocker.",
            result={"phase": "docs_index"},
            command_results=command_results,
            blockers=[{"phase": "docs_index", "message": str(exc)}],
            next_actions=["Fix docs indexing, then rerun prepare_start."],
        )

    status = workflow_status(task=task, module=module, root=str(root_path))
    return _tool_output(
        "prepare_start",
        input_summary,
        summary="Startup workflow completed.",
        result={
            "phase": "complete",
            "repo_health": status["result"]["repo_health"],
            "required_instruction_files": status["result"]["required_instruction_files"],
            "docs_index": {
                "index_dir": str(index.index_dir),
                "file_count": index.file_count,
                "chunk_count": index.chunk_count,
            },
            "metadata_status": status["result"]["metadata_status"],
            "recommended_checks": status["result"]["recommended_checks"],
        },
        command_results=command_results,
        next_actions=[
            "Read every required instruction file.",
            "Call docs(...) for focused RAG retrieval before normal repository inspection.",
            "Call workflow_status(...) before and after changes.",
        ],
    )


def docs(
    query: str,
    mode: str = "search",
    top_k: int = 5,
    index_dir: str = DEFAULT_INDEX_DIR,
) -> dict[str, Any]:
    """Search or answer from the local docs RAG index."""
    resolved_index_dir = _resolve_index_dir(index_dir)
    input_summary = {
        "query": query,
        "mode": mode,
        "top_k": top_k,
        "index_dir": index_dir,
        "resolved_index_dir": str(resolved_index_dir),
    }
    if mode not in {"search", "ask"}:
        return _tool_output(
            "docs",
            input_summary,
            ok=False,
            summary="Unsupported docs mode.",
            blockers=[{"phase": "validate", "message": "mode must be 'search' or 'ask'"}],
        )

    try:
        if mode == "search":
            results = docs_assistant.search_docs(query, index_dir=resolved_index_dir, top_k=top_k)
            result: dict[str, Any] = {
                "mode": mode,
                "results": [_search_result_to_dict(item) for item in results],
                "freshness_warnings": _freshness_warnings(resolved_index_dir),
            }
        else:
            answer = docs_assistant.ask_docs(
                query,
                index_dir=resolved_index_dir,
                top_k=top_k,
                no_llm=True,
            )
            result = {
                "mode": mode,
                "answer": answer.answer,
                "citations": answer.citations,
                "results": [_search_result_to_dict(item) for item in answer.results],
                "freshness_warnings": _freshness_warnings(resolved_index_dir),
            }
    except Exception as exc:
        return _tool_output(
            "docs",
            input_summary,
            ok=False,
            summary="Docs retrieval failed.",
            blockers=[{"phase": "docs", "message": str(exc)}],
            next_actions=["Run prepare_start(...) to rebuild the docs index, then retry docs(...)."],
        )

    return _tool_output(
        "docs",
        input_summary,
        summary=f"Docs {mode} completed.",
        result=result,
        next_actions=["Use cited files as focused context before repository inspection."],
    )


def workflow_status(
    task: str,
    module: str | None = None,
    change_type: str = "implementation",
    instructions_read: bool = False,
    root: str = ".",
) -> dict[str, Any]:
    """Return route, repository, metadata, and check status for the workflow."""
    root_path = _resolve_root(root)
    input_summary = {
        "task": task,
        "module": module,
        "change_type": change_type,
        "instructions_read": instructions_read,
        "root": str(root_path),
    }
    route = route_agent_context(task=task, module=module)
    health = repo_health(root=str(root_path))
    metadata = metadata_status(root=str(root_path))
    dependency_metadata = dependency_metadata_status(root=str(root_path))
    recommended = recommend_tests(area=module or task, change_type=change_type)
    missing = _missing_mandatory_actions(
        health=health,
        metadata=metadata,
        change_type=change_type,
        instructions_read=instructions_read,
        route=route,
        root=root_path,
    )
    ok = not metadata["blockers"] and not dependency_metadata["blockers"] and not missing
    return _tool_output(
        "workflow_status",
        input_summary,
        ok=ok,
        summary="Workflow status collected." if ok else "Workflow status requires action.",
        result={
            "repo_health": health,
            "required_instruction_files": route["required_files"],
            "routing": route,
            "metadata_status": metadata,
            "dependency_metadata_status": dependency_metadata,
            "recommended_checks": recommended,
            "missing_mandatory_actions": missing,
        },
        blockers=[*metadata["blockers"], *dependency_metadata["blockers"]],
        next_actions=_workflow_next_actions(missing),
    )


def version_bump(
    summary: str,
    change_type: str = "implementation",
    dry_run: bool = False,
    root: str = ".",
) -> dict[str, Any]:
    """Plan or apply the repository version bump and changelog update."""
    root_path = _resolve_root(root)
    input_summary = {
        "summary": summary,
        "change_type": change_type,
        "dry_run": dry_run,
        "root": str(root_path),
    }
    if _is_docs_only(change_type) and not _is_release_artifact(change_type):
        return _tool_output(
            "version_bump",
            input_summary,
            summary="No version bump is required for documentation-only changes.",
            result={"decision": "no_bump", "planned_version": None, "changelog_entry": None},
            next_actions=["Do not edit package metadata for ordinary documentation-only changes."],
        )

    current_version = _package_version(root_path)
    changelog = root_path / CHANGELOG_PATH
    changelog_text = _read_text(changelog)
    bullet = _format_changelog_bullet(summary)
    unreleased_count = _count_unreleased_changelog_bullets(changelog_text)
    planned_unreleased_count = unreleased_count + 1
    next_version_value = _increment_version(current_version)
    should_bump = planned_unreleased_count >= UNRELEASED_CHANGELOG_THRESHOLD
    if should_bump:
        entry = _format_changelog_entry(
            next_version_value,
            _unreleased_changelog_bullets(changelog_text) + [bullet],
        )
        planned = {
            "decision": "bump",
            "current_version": current_version,
            "planned_version": next_version_value,
            "changelog_entry": entry,
            "unreleased_count": planned_unreleased_count,
        }
    else:
        planned = {
            "decision": "unreleased",
            "current_version": current_version,
            "planned_version": None,
            "changelog_entry": bullet,
            "unreleased_count": planned_unreleased_count,
            "threshold": UNRELEASED_CHANGELOG_THRESHOLD,
        }
    if dry_run:
        summary_text = (
            "Version bump planned."
            if should_bump
            else "Unreleased changelog update planned; version bump threshold not reached."
        )
        return _tool_output(
            "version_bump",
            input_summary,
            summary=summary_text,
            result=planned,
            next_actions=["Run version_bump(..., dry_run=False) when ready to edit metadata."],
        )

    if not should_bump:
        try:
            updated_changelog = _upsert_unreleased_changelog_bullet(changelog_text, bullet)
        except ValueError as exc:
            return _tool_output(
                "version_bump",
                input_summary,
                ok=False,
                summary="Unreleased changelog update failed.",
                result=planned,
                blockers=[{"phase": "metadata", "message": str(exc)}],
                next_actions=["Fix the changelog, then rerun version_bump(...)."],
            )
        _write_text(changelog, updated_changelog)
        metadata = metadata_status(root=str(root_path))
        if not metadata["ok"]:
            return _tool_output(
                "version_bump",
                input_summary,
                ok=False,
                summary="Metadata is not aligned after unreleased changelog update.",
                result={**planned, "metadata_status": metadata},
                blockers=metadata["blockers"],
                next_actions=["Fix metadata alignment, then rerun workflow_status(...)."],
            )
        return _tool_output(
            "version_bump",
            input_summary,
            summary=(
                "Added unreleased changelog bullet "
                f"({planned_unreleased_count}/{UNRELEASED_CHANGELOG_THRESHOLD}); "
                "version not bumped."
            ),
            result={**planned, "metadata_status": metadata},
            next_actions=["Run workflow_status(...) and run_checks(...) before committing."],
        )

    pyproject = root_path / "pyproject.toml"
    readme = root_path / "README.md"
    try:
        pyproject_text = _replace_required(
            VERSION_RE,
            _read_text(pyproject),
            f'version = "{next_version_value}"',
            "project version",
        )
        readme_text = _replace_required(
            README_VERSION_RE,
            _read_text(readme),
            f"**Version:** `{next_version_value}`",
            "README version",
        )
        updated_changelog = _release_unreleased_changelog_text(changelog_text, entry)
    except ValueError as exc:
        return _tool_output(
            "version_bump",
            input_summary,
            ok=False,
            summary="Version bump metadata update failed.",
            result=planned,
            blockers=[{"phase": "metadata", "message": str(exc)}],
            next_actions=["Fix the metadata marker, then rerun version_bump(...)."],
        )

    _write_text(pyproject, pyproject_text)
    _write_text(readme, readme_text)
    _write_text(changelog, updated_changelog)
    metadata = metadata_status(root=str(root_path))
    if not metadata["ok"]:
        return _tool_output(
            "version_bump",
            input_summary,
            ok=False,
            summary="Version bump metadata is not aligned after update.",
            result={**planned, "metadata_status": metadata},
            blockers=metadata["blockers"],
            next_actions=["Fix metadata alignment, then rerun workflow_status(...)."],
        )
    return _tool_output(
        "version_bump",
        input_summary,
        summary=f"Bumped version to {next_version_value}.",
        result={**planned, "metadata_status": metadata},
        next_actions=["Run workflow_status(...) and run_checks(...) before committing."],
    )


def run_checks(
    area: str | None = None,
    change_type: str = "implementation",
    level: str = "focused",
    dry_run: bool = False,
    root: str = ".",
) -> dict[str, Any]:
    """Plan or execute focused, pre-commit, or release validation checks."""
    root_path = _resolve_root(root)
    input_summary = {
        "area": area,
        "change_type": change_type,
        "level": level,
        "dry_run": dry_run,
        "root": str(root_path),
    }
    try:
        commands = _check_commands(area=area, change_type=change_type, level=level)
    except ValueError as exc:
        return _tool_output(
            "run_checks",
            input_summary,
            ok=False,
            summary="Invalid check request.",
            blockers=[{"phase": "validate", "message": str(exc)}],
        )

    planned = [_command_display(command) for command in commands]
    if dry_run:
        return _tool_output(
            "run_checks",
            input_summary,
            summary="Check commands planned.",
            result={"planned_commands": planned},
            next_actions=["Run run_checks(..., dry_run=False) to execute these commands."],
        )

    command_results: list[dict[str, Any]] = []
    for command in commands:
        result = _run_command(root_path, command)
        command_results.append(result)
        if not result["ok"]:
            return _tool_output(
                "run_checks",
                input_summary,
                ok=False,
                summary="A validation command failed.",
                result={"planned_commands": planned},
                command_results=command_results,
                blockers=[_command_blocker("run_checks", result)],
                next_actions=["Fix the failure, then rerun the same check level."],
            )

    result_data: dict[str, Any] = {"planned_commands": planned}
    if level == "precommit":
        try:
            fingerprint = _working_tree_fingerprint(root_path)
        except _FingerprintError as exc:
            return _tool_output(
                "run_checks",
                input_summary,
                ok=False,
                summary="Validation commands completed, but working tree fingerprinting failed.",
                result=result_data,
                command_results=command_results,
                blockers=[exc.to_dict()],
                next_actions=["Fix the git fingerprinting failure, then rerun run_checks(level='precommit')."],
            )
        _record_precommit_success(root_path, fingerprint, command_results)
        result_data["precommit_fingerprint"] = fingerprint

    return _tool_output(
        "run_checks",
        input_summary,
        summary="Validation commands completed.",
        result=result_data,
        command_results=command_results,
        next_actions=["Call workflow_status(...) again before commit."],
    )


def git_workflow(
    action: str,
    message: str | None = None,
    paths: list[str] | None = None,
    root: str = ".",
) -> dict[str, Any]:
    """Run repository git workflow actions with structured blockers."""
    root_path = _resolve_root(root)
    input_summary = {
        "action": action,
        "message": message,
        "paths": paths,
        "root": str(root_path),
    }
    if action not in {"commit", "push"}:
        return _tool_output(
            "git_workflow",
            input_summary,
            ok=False,
            summary="Unsupported git workflow action.",
            blockers=[{"phase": "validate", "message": "action must be 'commit' or 'push'"}],
        )

    if action == "push":
        return _push_dev_workflow(root_path, input_summary)

    if not message:
        return _tool_output(
            "git_workflow",
            input_summary,
            ok=False,
            summary="Commit message is required.",
            blockers=[{"phase": "validate", "message": "message is required for commit"}],
        )
    path_validation = _validated_commit_paths(root_path, paths)
    commit_paths = path_validation["paths"]
    if path_validation["blockers"]:
        return _tool_output(
            "git_workflow",
            input_summary,
            ok=False,
            summary="Commit staging paths are not safe.",
            blockers=path_validation["blockers"],
            next_actions=["Pass explicit non-sensitive file paths for the current batch."],
        )
    if not commit_paths:
        return _tool_output(
            "git_workflow",
            input_summary,
            ok=False,
            summary="Commit staging requires explicit paths.",
            blockers=[
                {
                    "phase": "stage",
                    "message": "paths are required for commit so git_workflow does not stage unrelated changes",
                }
            ],
            next_actions=["Pass explicit paths for the current batch, then retry git_workflow(action='commit')."],
        )
    version_requirement = _version_bump_requirement(root_path, commit_paths)
    if version_requirement["missing"]:
        return _tool_output(
            "git_workflow",
            input_summary,
            ok=False,
            summary="Commit blocked because non-documentation changes need a version bump.",
            result={"version_bump_requirement": version_requirement},
            blockers=[
                {
                    "phase": "version_bump",
                    "message": _version_bump_message(version_requirement["missing"]),
                }
            ],
            next_actions=["Run version_bump(...), include all version metadata paths, then retry the commit."],
        )
    verification = _verify_precommit_success(root_path)
    if not verification["ok"]:
        return _tool_output(
            "git_workflow",
            input_summary,
            ok=False,
            summary="Commit blocked because pre-commit checks are not recorded for this tree.",
            result={"precommit_verification": verification},
            blockers=[{"phase": "precommit", "message": verification["message"]}],
            next_actions=["Run run_checks(level='precommit') before committing."],
        )

    add = _run_command(
        root_path,
        {
            "display": f"git add -- {' '.join(commit_paths)}",
            "args": ["git", "add", "--", *commit_paths],
            "env": {},
        },
    )
    if not add["ok"]:
        return _tool_output(
            "git_workflow",
            input_summary,
            ok=False,
            summary="git add failed.",
            command_results=[add],
            blockers=[_command_blocker("stage", add)],
        )
    commit = _run_command(
        root_path,
        {
            "display": f"git commit -m {message!r}",
            "args": ["git", "commit", "-m", message],
            "env": {},
        },
    )
    if not commit["ok"]:
        return _tool_output(
            "git_workflow",
            input_summary,
            ok=False,
            summary="Commit failed.",
            command_results=[add, commit],
            blockers=[_command_blocker("commit", commit)],
        )

    push = _push_dev_result(root_path)
    command_results = [add, commit, *push["command_results"]]
    if push["blockers"]:
        return _tool_output(
            "git_workflow",
            input_summary,
            ok=False,
            summary="Commit completed, but push to dev failed.",
            result={"push_readiness": push["readiness"]},
            command_results=command_results,
            blockers=push["blockers"],
            next_actions=["Resolve push blockers, then retry git_workflow(action='push')."],
        )

    return _tool_output(
        "git_workflow",
        input_summary,
        summary="Commit completed and pushed to dev.",
        result={"push_readiness": push["readiness"]},
        command_results=command_results,
    )


def release_workflow(action: str = "status", root: str = ".") -> dict[str, Any]:
    """Report release readiness or delegate to the release script."""
    root_path = _resolve_root(root)
    input_summary = {"action": action, "root": str(root_path)}
    if action not in {"status", "merge-dev", "publish"}:
        return _tool_output(
            "release_workflow",
            input_summary,
            ok=False,
            summary="Unsupported release workflow action.",
            blockers=[{"phase": "validate", "message": "action must be 'status', 'merge-dev', or 'publish'"}],
        )

    if action == "merge-dev":
        return _merge_dev_for_release(root_path, input_summary)

    if action == "status":
        status = _release_readiness(root_path)
        if status["blockers"]:
            return _tool_output(
                "release_workflow",
                input_summary,
                ok=False,
                summary="Release has blockers.",
                result=status,
                command_results=status["command_results"],
                blockers=status["blockers"],
                next_actions=["Resolve release blockers, then rerun release_workflow(action='status')."],
            )

        command_results: list[dict[str, Any]] = []
        for command in RELEASE_CHECK_COMMANDS:
            result = _run_command(root_path, command)
            command_results.append(result)
            if not result["ok"]:
                blocker = _command_blocker("release_checks", result)
                status["blockers"].append(blocker)
                return _tool_output(
                    "release_workflow",
                    input_summary,
                    ok=False,
                    summary="Release validation command failed.",
                    result=status,
                    command_results=[*status["command_results"], *command_results],
                    blockers=[blocker],
                    next_actions=["Fix the release validation failure, then rerun release_workflow(action='status')."],
                )

        try:
            fingerprint = _working_tree_fingerprint(root_path)
        except _FingerprintError as exc:
            return _tool_output(
                "release_workflow",
                input_summary,
                ok=False,
                summary="Release validation commands completed, but working tree fingerprinting failed.",
                result=status,
                command_results=[*status["command_results"], *command_results],
                blockers=[exc.to_dict()],
                next_actions=["Fix the git fingerprinting failure, then rerun release_workflow(action='status')."],
            )
        _record_release_check_success(root_path, fingerprint, command_results)
        status["release_check_verification"] = _verify_release_check_success(root_path)
        return _tool_output(
            "release_workflow",
            input_summary,
            summary="Release is ready.",
            result=status,
            command_results=[*status["command_results"], *command_results],
            next_actions=["Use release_workflow(action='publish') only after all blockers are resolved."],
        )

    status = _release_readiness(root_path, require_release_check=True)
    if status["blockers"]:
        return _tool_output(
            "release_workflow",
            input_summary,
            ok=False,
            summary="Publish blocked by release readiness checks.",
            result=status,
            command_results=status["command_results"],
            blockers=status["blockers"],
        )
    result = _run_command(
        root_path,
        {
            "display": "release_routines/pypi_release.sh",
            "args": ["release_routines/pypi_release.sh"],
            "env": {},
        },
    )
    return _tool_output(
        "release_workflow",
        input_summary,
        ok=result["ok"],
        summary="Release publish workflow completed." if result["ok"] else "Release publish workflow failed.",
        result=status,
        command_results=[*status["command_results"], result],
        blockers=[] if result["ok"] else [_command_blocker("publish", result)],
    )


def create_mcp_server() -> Any:
    """Create the MCP server instance."""
    if FastMCP is None:
        raise RuntimeError(
            "The MCP SDK is not installed. Run "
            "`.venv/bin/python -m pip install -r agent_tools/requirements-mcp.txt` "
            "from the repository root, then start this server again."
        )

    server = FastMCP("analytics-toolkit-agent-tools")

    server.tool()(prepare_start)
    server.tool()(docs)
    server.tool()(workflow_status)
    server.tool()(version_bump)
    server.tool()(run_checks)
    server.tool()(git_workflow)
    server.tool()(release_workflow)

    server.resource("repo://AGENTS.md")(_resource_file("AGENTS.md"))
    server.resource("repo://agent_tools/README.md")(_resource_file("agent_tools/README.md"))
    for path in sorted((REPO_ROOT / "agent_docs").glob("*.md")):
        rel_path = path.relative_to(REPO_ROOT).as_posix()
        server.resource(f"repo://{rel_path}")(_resource_file(rel_path))

    return server


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "call":
        return _handle_cli_call(args[1:])
    create_mcp_server().run()
    return 0


def route_agent_context(task: str, module: str | None = None) -> dict[str, Any]:
    """Return required instruction files for the requested repo work."""
    tokens = _tokens(f"{task} {module or ''}")
    required = ["AGENTS.md"]

    for token in tokens:
        doc = TASK_DOCS.get(token)
        if doc is not None:
            required.append(doc)

    module_doc = _module_doc(module)
    if module_doc is not None:
        required.append(module_doc)

    for token in tokens:
        doc = MODULE_DOCS.get(token)
        if doc is not None:
            required.append(doc)

    if len(required) == 1 and tokens:
        required.append("agent_docs/development.md")

    required = _unique(required)
    return {
        "task": task,
        "module": module,
        "required_files": required,
        "startup_sequence": [
            "Use prepare_start before repository search, file inspection, tests, or edits.",
            "Use docs(...) after prepare_start for focused RAG context.",
            "Read every required instruction file before normal repository inspection.",
        ],
        "notes": _routing_notes(required),
    }


def repo_health(root: str = ".") -> dict[str, Any]:
    """Return read-only repository status useful to coding agents."""
    root_path = _resolve_root(root)
    status = _run_git(root_path, ["status", "--short"])
    branch = _run_git(root_path, ["branch", "--show-current"])
    diff_stat = _run_git(root_path, ["diff", "--stat"])
    staged_diff_stat = _run_git(root_path, ["diff", "--cached", "--stat"])
    pyproject_text = _read_text(root_path / "pyproject.toml")
    changelog_text = _read_text(root_path / "docs" / "CHANGELOG.md")
    latest = _latest_changelog_entry(changelog_text)
    return {
        "root": str(root_path),
        "branch": branch["stdout"].strip() if branch["ok"] else "",
        "dirty": bool(status["stdout"].strip()) if status["ok"] else None,
        "status_short": status["stdout"].splitlines() if status["ok"] else [],
        "diff_stat": diff_stat["stdout"].splitlines() if diff_stat["ok"] else [],
        "staged_diff_stat": staged_diff_stat["stdout"].splitlines()
        if staged_diff_stat["ok"]
        else [],
        "package_version": _parse_required(VERSION_RE, pyproject_text, "project version"),
        "requires_python": _parse_required(PYTHON_REQUIRES_RE, pyproject_text, "requires-python"),
        "latest_changelog": latest,
        "ignored_local_state": [".venv/", ".rag_index/"],
    }


def metadata_status(root: str = ".") -> dict[str, Any]:
    root_path = _resolve_root(root)
    package_version = _package_version(root_path)
    readme_version = _readme_version(root_path)
    latest = _latest_changelog_entry(_read_text(root_path / "docs" / "CHANGELOG.md"))
    blockers = []
    if readme_version != package_version:
        blockers.append(
            {
                "phase": "metadata",
                "message": "README version does not match pyproject.toml",
                "pyproject_version": package_version,
                "readme_version": readme_version,
            }
        )
    if latest.get("version") != package_version:
        blockers.append(
            {
                "phase": "metadata",
                "message": "Latest changelog version does not match pyproject.toml",
                "pyproject_version": package_version,
                "latest_changelog": latest,
            }
        )
    return {
        "ok": not blockers,
        "package_version": package_version,
        "readme_version": readme_version,
        "latest_changelog": latest,
        "blockers": blockers,
    }


def dependency_metadata_status(root: str = ".") -> dict[str, Any]:
    root_path = _resolve_root(root)
    blockers = []
    project: dict[str, Any] = {}
    try:
        project = load_project(root_path / "pyproject.toml")
        failures = validate_readme_dependencies(
            project,
            _read_text(root_path / "README.md"),
        )
    except (OSError, SystemExit, ValueError) as exc:
        failures = [str(exc)]

    for failure in failures:
        blockers.append({"phase": "dependency_metadata", "message": failure})

    pyproject_deps = list(project.get("dependencies", []))
    optional_deps = project.get("optional-dependencies", {})
    optional_count = (
        sum(len(values) for values in optional_deps.values())
        if isinstance(optional_deps, dict)
        else 0
    )
    return {
        "ok": not blockers,
        "dependency_count": len(pyproject_deps),
        "optional_dependency_count": optional_count,
        "blockers": blockers,
    }


def next_version(current_version: str | None = None, root: str = ".") -> dict[str, Any]:
    """Internal compatibility helper for version planning."""
    if current_version is None:
        current_version = _package_version(_resolve_root(root))
    return {"current_version": current_version, "next_version": _increment_version(current_version)}


def changelog_status(root: str = ".") -> dict[str, Any]:
    """Internal compatibility helper for changelog metadata status."""
    status = metadata_status(root=root)
    return {
        "package_version": status["package_version"],
        "latest_changelog": status["latest_changelog"],
        "matches": status["latest_changelog"].get("version") == status["package_version"],
    }


def recommend_tests(area: str | None, change_type: str = "implementation") -> dict[str, Any]:
    """Recommend focused commands for the requested area."""
    key = _normalize_area(area or "")
    commands = TEST_COMMANDS.get(key, [])
    if not commands:
        commands = [
            {
                "display": "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q",
                "args": ["pytest", "-q"],
                "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
            }
        ]

    required_final = [] if _is_docs_only(change_type) else [_command_display(PRECOMMIT_COMMAND)]
    return {
        "area": area,
        "change_type": change_type,
        "focused_commands": [_command_display(command) for command in commands],
        "required_final_commands": required_final,
        "notes": [
            "Do not run tests against real databases.",
            "Use fake connections, monkeypatching, and tests/conftest.py fixtures.",
        ],
    }


def _handle_cli_call(argv: list[str]) -> int:
    parser = _build_cli_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "tool": args.command.replace("-", "_") if hasattr(args, "command") else "unknown",
                    "error": str(exc),
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent_tools/mcp_tool.sh")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare-start")
    prepare_parser.add_argument("--task", required=True)
    prepare_parser.add_argument("--module")
    prepare_parser.add_argument("--root", default=".")
    prepare_parser.add_argument("--index-dir", default=DEFAULT_INDEX_DIR)
    prepare_parser.add_argument("--ensure-project-env", dest="ensure_project_env", action="store_true", default=True)
    prepare_parser.add_argument("--no-ensure-project-env", dest="ensure_project_env", action="store_false")
    prepare_parser.set_defaults(
        handler=lambda args: prepare_start(
            task=args.task,
            module=args.module,
            root=args.root,
            index_dir=args.index_dir,
            ensure_project_env=args.ensure_project_env,
        )
    )

    docs_parser = subparsers.add_parser("docs")
    docs_parser.add_argument("query")
    docs_parser.add_argument("--mode", choices=["search", "ask"], default="search")
    docs_parser.add_argument("--top-k", type=int, default=5)
    docs_parser.add_argument("--index-dir", default=DEFAULT_INDEX_DIR)
    docs_parser.set_defaults(
        handler=lambda args: docs(
            query=args.query,
            mode=args.mode,
            top_k=args.top_k,
            index_dir=args.index_dir,
        )
    )

    workflow_parser = subparsers.add_parser("workflow-status")
    workflow_parser.add_argument("--task", required=True)
    workflow_parser.add_argument("--module")
    workflow_parser.add_argument("--change-type", default="implementation")
    workflow_parser.add_argument("--instructions-read", action="store_true")
    workflow_parser.add_argument("--root", default=".")
    workflow_parser.set_defaults(
        handler=lambda args: workflow_status(
            task=args.task,
            module=args.module,
            change_type=args.change_type,
            instructions_read=args.instructions_read,
            root=args.root,
        )
    )

    bump_parser = subparsers.add_parser("version-bump")
    bump_parser.add_argument("summary")
    bump_parser.add_argument("--change-type", default="implementation")
    bump_parser.add_argument("--dry-run", action="store_true")
    bump_parser.add_argument("--root", default=".")
    bump_parser.set_defaults(
        handler=lambda args: version_bump(
            summary=args.summary,
            change_type=args.change_type,
            dry_run=args.dry_run,
            root=args.root,
        )
    )

    checks_parser = subparsers.add_parser("run-checks")
    checks_parser.add_argument("--area")
    checks_parser.add_argument("--change-type", default="implementation")
    checks_parser.add_argument("--level", choices=["focused", "precommit", "release"], default="focused")
    checks_parser.add_argument("--dry-run", action="store_true")
    checks_parser.add_argument("--root", default=".")
    checks_parser.set_defaults(
        handler=lambda args: run_checks(
            area=args.area,
            change_type=args.change_type,
            level=args.level,
            dry_run=args.dry_run,
            root=args.root,
        )
    )

    git_parser = subparsers.add_parser("git-workflow")
    git_parser.add_argument("action", choices=["commit", "push"])
    git_parser.add_argument("--message")
    git_parser.add_argument("--path", dest="paths", action="append")
    git_parser.add_argument("--root", default=".")
    git_parser.set_defaults(
        handler=lambda args: git_workflow(
            action=args.action,
            message=args.message,
            paths=args.paths,
            root=args.root,
        )
    )

    release_parser = subparsers.add_parser("release-workflow")
    release_parser.add_argument("--action", choices=["status", "merge-dev", "publish"], default="status")
    release_parser.add_argument("--root", default=".")
    release_parser.set_defaults(
        handler=lambda args: release_workflow(action=args.action, root=args.root)
    )

    return parser


def _prepare_start_commands(root: Path, ensure_project_env: bool) -> list[tuple[str, dict[str, Any]]]:
    venv_python = root / ".venv" / "bin" / "python"
    commands: list[tuple[str, dict[str, Any]]] = [
        (
            "git_fetch_dev",
            {
                "display": f"git fetch origin {WORK_BRANCH}",
                "args": ["git", "fetch", "origin", WORK_BRANCH],
                "env": {},
            },
        ),
        (
            "git_switch_dev",
            {
                "display": f"git switch {WORK_BRANCH}",
                "args": ["git", "switch", WORK_BRANCH],
                "env": {},
            },
        ),
        (
            "git_pull_dev",
            {
                "display": f"git pull --ff-only origin {WORK_BRANCH}",
                "args": ["git", "pull", "--ff-only", "origin", WORK_BRANCH],
                "env": {},
            },
        ),
    ]
    if not venv_python.exists():
        commands.append(
            (
                "venv",
                {
                    "display": "python -m venv .venv",
                    "args": [sys.executable, "-m", "venv", ".venv"],
                    "env": {},
                },
            )
        )
    commands.append(
        (
            "mcp_requirements",
            {
                "display": ".venv/bin/python -m pip install -r agent_tools/requirements-mcp.txt",
                "args": [str(venv_python), "-m", "pip", "install", "-r", "agent_tools/requirements-mcp.txt"],
                "env": {},
            },
        )
    )
    if ensure_project_env:
        commands.append(
            (
                "project_env",
                {
                    "display": ".venv/bin/python -m pip install -e . pytest tox",
                    "args": [str(venv_python), "-m", "pip", "install", "-e", ".", "pytest", "tox"],
                    "env": {},
                },
            )
        )
    return commands


def _check_commands(area: str | None, change_type: str, level: str) -> list[dict[str, Any]]:
    if level == "focused":
        key = _normalize_area(area or "")
        return TEST_COMMANDS.get(
            key,
            [
                {
                    "display": "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q",
                    "args": ["pytest", "-q"],
                    "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
                }
            ],
        )
    if level == "precommit":
        return [PRECOMMIT_COMMAND]
    if level == "release":
        return RELEASE_CHECK_COMMANDS
    raise ValueError("level must be 'focused', 'precommit', or 'release'")


def _run_command(root: Path, command: dict[str, Any]) -> dict[str, Any]:
    env = os.environ.copy()
    env.update(command.get("env") or {})
    try:
        completed = subprocess.run(
            command["args"],
            cwd=str(root),
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
    except OSError as exc:
        return {
            "ok": False,
            "command": _command_display(command),
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "summary": str(exc),
        }
    return {
        "ok": completed.returncode == 0,
        "command": _command_display(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "summary": _command_summary(completed.stdout, completed.stderr),
    }


def _run_git_pull(root: Path) -> dict[str, Any]:
    return _run_git(root, ["pull", "origin", "main"])


def _run_git(root: Path, args: list[str]) -> dict[str, Any]:
    return _run_command(root, {"display": f"git {' '.join(args)}", "args": ["git", *args], "env": {}})


def _command_display(command: dict[str, Any]) -> str:
    return str(command["display"])


def _command_summary(stdout: str, stderr: str, max_chars: int = 500) -> str:
    text = (stdout.strip() or stderr.strip()).replace("\n", " ")
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def _command_blocker(phase: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": phase,
        "command": result.get("command"),
        "returncode": result.get("returncode"),
        "stderr": result.get("stderr", "").strip(),
        "stdout": result.get("stdout", "").strip(),
    }


def _tool_output(
    tool: str,
    input_summary: dict[str, Any],
    *,
    ok: bool = True,
    summary: str = "",
    result: dict[str, Any] | None = None,
    command_results: list[dict[str, Any]] | None = None,
    blockers: list[dict[str, Any]] | None = None,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "tool": tool,
        "input": input_summary,
        "summary": summary,
        "result": result or {},
        "command_results": command_results or [],
        "blockers": blockers or [],
        "next_actions": next_actions or [],
    }


def _search_result_to_dict(result: docs_assistant.SearchResult) -> dict[str, Any]:
    chunk = result.chunk
    return {
        "score": result.score,
        "lexical_score": result.lexical_score,
        "citation": chunk.citation,
        "path": chunk.path,
        "heading": chunk.heading,
        "source_type": chunk.source_type,
        "snippet": docs_assistant.snippet(chunk.text),
    }


def _freshness_warnings(index_dir: str | Path) -> list[str]:
    try:
        return docs_assistant.index_freshness_warnings(index_dir)
    except (FileNotFoundError, ValueError) as exc:
        return [str(exc)]


def _resource_file(rel_path: str) -> Any:
    def read_resource() -> str:
        path = REPO_ROOT / rel_path
        return path.read_text(encoding="utf-8")

    read_resource.__name__ = f"read_{rel_path.replace('/', '_').replace('.', '_')}"
    return read_resource


def _resolve_root(root: str) -> Path:
    if root in {"", "."}:
        return REPO_ROOT.resolve()
    path = Path(root)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def _resolve_index_dir(index_dir: str | Path) -> Path:
    path = Path(index_dir)
    if path.is_absolute():
        return path
    return (REPO_ROOT / path).resolve()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(text, encoding="utf-8")
    os.replace(temp_path, path)


def _parse_required(pattern: re.Pattern[str], text: str, label: str) -> str:
    match = pattern.search(text)
    if match is None:
        raise ValueError(f"Could not parse {label}")
    return match.group(1)


def _replace_required(pattern: re.Pattern[str], text: str, replacement: str, label: str) -> str:
    updated, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise ValueError(f"Could not update {label}")
    return updated


def _package_version(root: Path) -> str:
    return _parse_required(VERSION_RE, _read_text(root / "pyproject.toml"), "project version")


def _readme_version(root: Path) -> str:
    return _parse_required(README_VERSION_RE, _read_text(root / "README.md"), "README version")


def _latest_changelog_entry(text: str) -> dict[str, str]:
    match = CHANGELOG_HEADING_RE.search(text)
    if match is None:
        return {}
    return {"version": match.group(1), "date": match.group(2)}


def _format_changelog_bullet(summary: str) -> str:
    clean_summary = summary.strip().rstrip(".")
    if not clean_summary:
        clean_summary = "Updated repository workflow."
    return f"- {clean_summary}."


def _format_changelog_entry(version: str, summary_or_bullets: str | list[str]) -> str:
    if isinstance(summary_or_bullets, str):
        bullets = [_format_changelog_bullet(summary_or_bullets)]
    else:
        bullets = summary_or_bullets or [_format_changelog_bullet("")]
    return f"## {version} - {date.today().isoformat()}\n\n" + "\n".join(bullets) + "\n"


def _prepend_changelog_entry(path: Path, entry: str) -> None:
    _write_text(path, _prepend_changelog_entry_text(_read_text(path), entry))


def _prepend_changelog_entry_text(text: str, entry: str) -> str:
    if not text.strip():
        raise ValueError("Could not update changelog")
    match = re.search(r"^##\s+", text, flags=re.MULTILINE)
    if match is None:
        return text.rstrip() + "\n\n" + entry
    return text[: match.start()] + entry + "\n" + text[match.start() :]


def _unreleased_changelog_bounds(text: str) -> tuple[int, int, int] | None:
    match = UNRELEASED_HEADING_RE.search(text)
    if match is None:
        return None
    next_match = re.search(r"^##\s+", text[match.end() :], flags=re.MULTILINE)
    end = len(text) if next_match is None else match.end() + next_match.start()
    return match.start(), match.end(), end


def _unreleased_changelog_bullets(text: str) -> list[str]:
    bounds = _unreleased_changelog_bounds(text)
    if bounds is None:
        return []
    _, body_start, section_end = bounds
    return [
        line.strip()
        for line in text[body_start:section_end].splitlines()
        if line.strip().startswith("- ")
    ]


def _count_unreleased_changelog_bullets(text: str) -> int:
    return len(_unreleased_changelog_bullets(text))


def _upsert_unreleased_changelog_bullet(text: str, bullet: str) -> str:
    if not text.strip():
        raise ValueError("Could not update changelog")
    bounds = _unreleased_changelog_bounds(text)
    if bounds is None:
        entry = f"## Unreleased\n\n{bullet}\n\n"
        match = re.search(r"^##\s+", text, flags=re.MULTILINE)
        if match is None:
            return text.rstrip() + "\n\n" + entry.rstrip() + "\n"
        return text[: match.start()] + entry + text[match.start() :]

    section_start, body_start, section_end = bounds
    body = text[body_start:section_end].strip()
    updated_body = f"{body}\n{bullet}" if body else bullet
    return (
        text[:body_start].rstrip()
        + "\n\n"
        + updated_body
        + "\n\n"
        + text[section_end:].lstrip("\n")
    )


def _release_unreleased_changelog_text(text: str, entry: str) -> str:
    bounds = _unreleased_changelog_bounds(text)
    if bounds is None:
        return _prepend_changelog_entry_text(text, entry)
    section_start, _, section_end = bounds
    without_unreleased = text[:section_start] + text[section_end:].lstrip("\n")
    return _prepend_changelog_entry_text(without_unreleased, entry)


def _increment_version(version: str) -> str:
    parts = version.split(".")
    if len(parts) != 4:
        raise ValueError("Version must have four numeric parts")
    numbers = [int(part) for part in parts]
    if any(number < 0 or number > 19 for number in numbers):
        raise ValueError("Version components must be between 0 and 19")

    for index in range(3, -1, -1):
        if numbers[index] < 19:
            numbers[index] += 1
            for reset_index in range(index + 1, 4):
                numbers[reset_index] = 0
            return ".".join(str(number) for number in numbers)
    raise ValueError("Version cannot be incremented without exceeding component limits")


def _project_dependencies(pyproject_text: str) -> list[str]:
    match = re.search(r"^dependencies\s*=\s*\[(.*?)^\]", pyproject_text, flags=re.MULTILINE | re.DOTALL)
    if match is None:
        return []
    return DEPENDENCY_RE.findall(match.group(1))


def _optional_dependencies(pyproject_text: str) -> dict[str, list[str]]:
    optional: dict[str, list[str]] = {}
    match = re.search(r"^\[project\.optional-dependencies\]\s*(.*?)(?:^\[|\Z)", pyproject_text, flags=re.MULTILINE | re.DOTALL)
    if match is None:
        return optional
    for line in match.group(1).splitlines():
        if "=" not in line:
            continue
        extra, values = line.split("=", 1)
        optional[extra.strip()] = DEPENDENCY_RE.findall(values)
    return optional


def _dependency_name(requirement: str) -> str:
    return re.split(r"[<>=!~;\[]+", requirement, maxsplit=1)[0].strip()


def _module_doc(module: str | None) -> str | None:
    if module is None:
        return None
    return MODULE_DOCS.get(_normalize_area(module))


def _normalize_area(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return {"ab": "ab_utils", "date": "dates", "docs": "documentation"}.get(normalized, normalized)


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", value.lower().replace("-", "_"))


def _unique(values: list[str]) -> list[str]:
    seen = set()
    unique = []
    for value in values:
        if value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def _routing_notes(required_files: list[str]) -> list[str]:
    notes = []
    if "agent_docs/development.md" in required_files:
        notes.append("Implementation, testing, build, or commit work requires development instructions.")
    if "agent_docs/documentation.md" in required_files:
        notes.append("Public documentation changes must follow documentation instructions.")
    if "agent_docs/release.md" in required_files:
        notes.append("Release and PyPI work must follow release instructions.")
    if "agent_docs/sql.md" in required_files:
        notes.append("SQL work must not run tests against real databases.")
    return notes


def _is_docs_only(change_type: str) -> bool:
    normalized = _normalize_area(change_type)
    return normalized in {"documentation", "docs", "documentation_only", "docs_only"}


def _is_release_artifact(change_type: str) -> bool:
    normalized = _normalize_area(change_type)
    return normalized in {"release", "release_artifact", "release_preparation", "pypi"}


def _missing_mandatory_actions(
    *,
    health: dict[str, Any],
    metadata: dict[str, Any],
    change_type: str,
    instructions_read: bool,
    route: dict[str, Any],
    root: Path,
) -> list[str]:
    missing = []
    if not instructions_read:
        missing.append("Read required instruction files: " + ", ".join(route["required_files"]))
    if health["dirty"] and not _is_docs_only(change_type):
        version_requirement = _version_bump_requirement(root)
        if version_requirement["missing"]:
            missing.append(_version_bump_message(version_requirement["missing"]))
        if not metadata["ok"]:
            missing.append("Run version_bump(...) so pyproject.toml, README.md, and docs/CHANGELOG.md align.")
        if not _verify_precommit_success(root)["ok"]:
            missing.append("Run run_checks(level='precommit') before git_workflow(action='commit').")
    return missing


def _workflow_next_actions(missing: list[str]) -> list[str]:
    if missing:
        return missing
    return ["Proceed with the next requested repository workflow step."]


def _working_tree_fingerprint(root: Path) -> str:
    parts = []
    for args in (
        ["rev-parse", "HEAD"],
        ["status", "--short"],
    ):
        result = _require_git_for_fingerprint(root, args)
        parts.append(result.get("stdout", ""))
        parts.append(result.get("stderr", ""))
    parts.extend(_tracked_diff_fingerprint_parts(root, staged=False))
    parts.extend(_tracked_diff_fingerprint_parts(root, staged=True))
    parts.extend(_untracked_file_fingerprint_parts(root))
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _validated_commit_paths(root: Path, paths: list[str] | None) -> dict[str, Any]:
    blockers: list[dict[str, str]] = []
    valid_paths: list[str] = []
    if not paths:
        return {"paths": valid_paths, "blockers": blockers}

    for raw_path in paths:
        path = raw_path.strip()
        if not path:
            continue
        blocker = _commit_path_blocker(root, path)
        if blocker is not None:
            blockers.append(blocker)
            continue
        rel_path = Path(path).as_posix().strip("/")
        if rel_path not in valid_paths:
            valid_paths.append(rel_path)
    return {"paths": valid_paths, "blockers": blockers}


def _commit_path_blocker(root: Path, rel_path: str) -> dict[str, str] | None:
    if rel_path in {".", "./"}:
        return _path_blocker(rel_path, "path must be a file path, not the repository root")
    if rel_path.startswith(":"):
        return _path_blocker(rel_path, "git pathspec magic is not allowed")
    if any(char in rel_path for char in "*?["):
        return _path_blocker(rel_path, "glob-style pathspecs are not allowed")

    path = Path(rel_path)
    if path.is_absolute():
        return _path_blocker(rel_path, "absolute paths are not allowed")
    if ".." in path.parts:
        return _path_blocker(rel_path, "paths must not escape the repository")

    normalized = path.as_posix().strip("/")
    if not normalized:
        return _path_blocker(rel_path, "path must not be empty")
    resolved = (root / normalized).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return _path_blocker(rel_path, "paths must stay inside the repository")
    if resolved == root or resolved.is_dir():
        return _path_blocker(rel_path, "path must be an explicit file path")
    if _is_sensitive_local_path(normalized):
        return _path_blocker(rel_path, "sensitive local paths must never be staged")
    return None


def _path_blocker(rel_path: str, message: str) -> dict[str, str]:
    return {"phase": "stage", "path": rel_path, "message": message}


def _changed_repo_paths(root: Path) -> set[str]:
    status = _run_git(root, ["status", "--short"])
    if not status["ok"]:
        return set()
    paths: set[str] = set()
    for line in status["stdout"].splitlines():
        if len(line) < 4:
            continue
        status_code = line[:2]
        path_text = line[3:].strip()
        if " -> " in path_text:
            paths.update(part.strip() for part in path_text.split(" -> ", 1) if part.strip())
        elif status_code == "??" and path_text.endswith("/"):
            paths.update(_untracked_paths_under(root, path_text))
        elif path_text:
            paths.add(path_text)
    return paths


def _untracked_paths_under(root: Path, rel_path: str) -> set[str]:
    result = _run_git(root, ["ls-files", "--others", "--exclude-standard", "--", rel_path])
    if not result["ok"]:
        return {rel_path}
    return set(path for path in result["stdout"].splitlines() if path) or {rel_path}


def _version_bump_requirement(root: Path, paths: list[str] | None = None) -> dict[str, Any]:
    changed_paths = _changed_repo_paths(root)
    selected_paths = set(paths or changed_paths)
    non_documentation_paths = sorted(
        path
        for path in selected_paths
        if not _is_sensitive_local_path(path)
        and path not in REQUIRED_VERSION_PATHS
        and not _is_documentation_path(path)
    )
    unreleased_count = 0
    required_paths: set[str] = {CHANGELOG_PATH}
    changelog_path = root / CHANGELOG_PATH
    if changelog_path.exists():
        unreleased_count = _count_unreleased_changelog_bullets(_read_text(changelog_path))
    if unreleased_count >= UNRELEASED_CHANGELOG_THRESHOLD:
        required_paths = set(REQUIRED_VERSION_PATHS)
    missing = sorted(
        path for path in required_paths if path not in changed_paths or path not in selected_paths
    )
    if not non_documentation_paths:
        missing = []
    return {
        "changed_paths": sorted(changed_paths),
        "selected_paths": sorted(selected_paths),
        "non_documentation_paths": non_documentation_paths,
        "required_paths": sorted(required_paths),
        "missing": missing,
        "unreleased_count": unreleased_count,
        "threshold": UNRELEASED_CHANGELOG_THRESHOLD,
    }


def _version_bump_message(missing: list[str]) -> str:
    return (
        "Run version_bump(...) so non-documentation changes include required version/changelog paths: "
        + ", ".join(missing)
        + "."
    )


def _is_documentation_path(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/").strip("/")
    return (
        normalized == "README.md"
        or normalized == "AGENTS.md"
        or normalized == "agent_tools/README.md"
        or normalized.startswith("docs/")
        or normalized.startswith("agent_docs/")
        or (normalized.endswith(".md") and "/" not in normalized)
    )


def _is_sensitive_local_path(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/").strip("/")
    if not normalized:
        return False
    parts = normalized.split("/")
    return normalized in SENSITIVE_LOCAL_PATHS or any(_is_sensitive_path_part(part) for part in parts)


def _is_sensitive_path_part(part: str) -> bool:
    return (
        part in SENSITIVE_LOCAL_PATHS
        or part in SENSITIVE_LOCAL_DIRS
        or part.startswith(".env.")
        or part.startswith(".env-")
    )


def _tracked_diff_fingerprint_parts(root: Path, *, staged: bool) -> list[str]:
    label = "staged-diff" if staged else "working-diff"
    name_only_args = ["diff", "--cached", "--name-only"] if staged else ["diff", "--name-only"]
    changed = _require_git_for_fingerprint(root, name_only_args)
    parts = [label, changed.get("stderr", "")]
    for rel_path in sorted(path for path in changed.get("stdout", "").splitlines() if path):
        if _is_sensitive_local_path(rel_path):
            parts.append(f"{rel_path}:excluded-sensitive-local-path")
            continue
        diff_args = ["diff", "--cached", "--binary", "--", rel_path] if staged else ["diff", "--binary", "--", rel_path]
        diff = _require_git_for_fingerprint(root, diff_args)
        parts.append(diff.get("stdout", ""))
        parts.append(diff.get("stderr", ""))
    return parts


def _untracked_file_fingerprint_parts(root: Path) -> list[str]:
    result = _require_git_for_fingerprint(root, ["ls-files", "--others", "--exclude-standard"])
    parts = ["untracked-files", result.get("stderr", "")]
    for rel_path in sorted(result.get("stdout", "").splitlines()):
        if not rel_path:
            continue
        if _is_sensitive_local_path(rel_path):
            parts.append(f"{rel_path}:excluded-sensitive-local-path")
            continue
        path = root / rel_path
        if not path.is_file():
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            digest = f"unreadable:{type(exc).__name__}:{exc}"
        parts.append(f"{rel_path}:{digest}")
    return parts


def _require_git_for_fingerprint(root: Path, args: list[str]) -> dict[str, Any]:
    result = _run_git(root, args)
    if not result["ok"]:
        raise _FingerprintError("git " + " ".join(args), result)
    return result


def _record_precommit_success(
    root: Path,
    fingerprint: str,
    command_results: list[dict[str, Any]],
) -> None:
    state_path = root / CHECK_STATE_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fingerprint": fingerprint,
        "recorded_at": date.today().isoformat(),
        "commands": [
            {
                "command": result["command"],
                "returncode": result["returncode"],
                "summary": result["summary"],
            }
            for result in command_results
        ],
    }
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _verify_precommit_success(root: Path) -> dict[str, Any]:
    state_path = root / CHECK_STATE_FILE
    if not state_path.is_file():
        return {"ok": False, "message": "No successful pre-commit check record exists."}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "message": f"Pre-commit check record is invalid: {exc}"}
    try:
        current = _working_tree_fingerprint(root)
    except _FingerprintError as exc:
        return {
            "ok": False,
            "message": str(exc),
            "fingerprint_error": exc.to_dict(),
        }
    recorded = state.get("fingerprint")
    if recorded != current:
        return {
            "ok": False,
            "message": "Recorded pre-commit check fingerprint does not match the current tree.",
            "recorded_fingerprint": recorded,
            "current_fingerprint": current,
        }
    return {"ok": True, "message": "Pre-commit check record matches the current tree.", "fingerprint": current}


def _push_readiness(root: Path) -> dict[str, Any]:
    health = repo_health(root=str(root))
    blockers: list[dict[str, Any]] = []
    if health["branch"] != WORK_BRANCH:
        blockers.append({"phase": "push", "message": f"Push workflow must run from {WORK_BRANCH}."})
    if health["dirty"]:
        blockers.append({"phase": "push", "message": "Push workflow requires a clean working tree."})

    remote = _remote_dev_status(root, require_equal=False)
    blockers.extend(remote["blockers"])
    return {
        "repo_health": health,
        "remote_dev_status": remote["result"],
        "command_results": remote["command_results"],
        "blockers": blockers,
    }


def _push_dev_result(root: Path) -> dict[str, Any]:
    readiness = _push_readiness(root)
    if readiness["blockers"]:
        return {
            "readiness": readiness,
            "command_results": readiness["command_results"],
            "blockers": readiness["blockers"],
        }

    result = _run_command(
        root,
        {
            "display": f"git push origin HEAD:{WORK_BRANCH}",
            "args": ["git", "push", "origin", f"HEAD:{WORK_BRANCH}"],
            "env": {},
        },
    )
    blockers = [] if result["ok"] else [_command_blocker("push", result)]
    return {
        "readiness": readiness,
        "command_results": [*readiness["command_results"], result],
        "blockers": blockers,
    }


def _push_dev_workflow(root: Path, input_summary: dict[str, Any]) -> dict[str, Any]:
    push = _push_dev_result(root)
    return _tool_output(
        "git_workflow",
        input_summary,
        ok=not push["blockers"],
        summary="Push completed." if not push["blockers"] else "Push failed.",
        result={"push_readiness": push["readiness"]},
        command_results=push["command_results"],
        blockers=push["blockers"],
        next_actions=[] if not push["blockers"] else ["Resolve push readiness blockers, then retry git_workflow(action='push')."],
    )


def _remote_main_status(root: Path, *, require_equal: bool) -> dict[str, Any]:
    return _remote_branch_status(root, branch=RELEASE_BRANCH, require_equal=require_equal)


def _remote_dev_status(root: Path, *, require_equal: bool) -> dict[str, Any]:
    return _remote_branch_status(root, branch=WORK_BRANCH, require_equal=require_equal)


def _remote_branch_status(root: Path, *, branch: str, require_equal: bool) -> dict[str, Any]:
    command_results: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    remote_ref = f"origin/{branch}"
    result_key = f"origin_{branch}"

    fetch = _run_git(root, ["fetch", "origin", branch])
    command_results.append(fetch)
    if not fetch["ok"]:
        blockers.append(_command_blocker(f"remote_{branch}", fetch))
        return {
            "result": {"fetched": False, "require_equal": require_equal},
            "command_results": command_results,
            "blockers": blockers,
        }

    head = _run_git(root, ["rev-parse", "HEAD"])
    origin = _run_git(root, ["rev-parse", remote_ref])
    command_results.extend([head, origin])
    if not head["ok"]:
        blockers.append(_command_blocker(f"remote_{branch}", head))
    if not origin["ok"]:
        blockers.append(_command_blocker(f"remote_{branch}", origin))
    if blockers:
        return {
            "result": {"fetched": True, "require_equal": require_equal},
            "command_results": command_results,
            "blockers": blockers,
        }

    head_commit = head["stdout"].strip()
    origin_commit = origin["stdout"].strip()
    result = {
        "fetched": True,
        "require_equal": require_equal,
        "head": head_commit,
        result_key: origin_commit,
        f"matches_origin_{branch}": head_commit == origin_commit,
    }
    if require_equal:
        if head_commit != origin_commit:
            blockers.append(
                {
                    "phase": f"remote_{branch}",
                    "message": f"Local HEAD must match {remote_ref}.",
                    "head": head_commit,
                    result_key: origin_commit,
                }
            )
        return {"result": result, "command_results": command_results, "blockers": blockers}

    ancestor = _run_git(root, ["merge-base", "--is-ancestor", remote_ref, "HEAD"])
    command_results.append(ancestor)
    result[f"contains_origin_{branch}"] = ancestor["returncode"] == 0
    if ancestor["returncode"] == 1:
        blockers.append(
            {
                "phase": f"remote_{branch}",
                "message": f"Local HEAD does not contain {remote_ref}; pull, rebase, or merge before continuing.",
                "head": head_commit,
                result_key: origin_commit,
            }
        )
    elif not ancestor["ok"]:
        blockers.append(_command_blocker(f"remote_{branch}", ancestor))
    return {"result": result, "command_results": command_results, "blockers": blockers}


def _merge_dev_for_release(root: Path, input_summary: dict[str, Any]) -> dict[str, Any]:
    health = repo_health(root=str(root))
    if health["dirty"]:
        return _tool_output(
            "release_workflow",
            input_summary,
            ok=False,
            summary="Release merge blocked by repository readiness checks.",
            result={"repo_health": health},
            blockers=[{"phase": "release_merge", "message": "Release merge requires a clean working tree."}],
        )

    command_results: list[dict[str, Any]] = []
    commands = [
        {
            "display": f"git fetch origin {RELEASE_BRANCH}",
            "args": ["git", "fetch", "origin", RELEASE_BRANCH],
            "env": {},
        },
        {
            "display": f"git fetch origin {WORK_BRANCH}",
            "args": ["git", "fetch", "origin", WORK_BRANCH],
            "env": {},
        },
        {
            "display": f"git switch {RELEASE_BRANCH}",
            "args": ["git", "switch", RELEASE_BRANCH],
            "env": {},
        },
        {
            "display": f"git pull --ff-only origin {RELEASE_BRANCH}",
            "args": ["git", "pull", "--ff-only", "origin", RELEASE_BRANCH],
            "env": {},
        },
        {
            "display": f"git merge --ff-only origin/{WORK_BRANCH}",
            "args": ["git", "merge", "--ff-only", f"origin/{WORK_BRANCH}"],
            "env": {},
        },
        {
            "display": f"git push origin {RELEASE_BRANCH}",
            "args": ["git", "push", "origin", RELEASE_BRANCH],
            "env": {},
        },
    ]
    for command in commands:
        result = _run_command(root, command)
        command_results.append(result)
        if not result["ok"]:
            return _tool_output(
                "release_workflow",
                input_summary,
                ok=False,
                summary="Release merge failed.",
                command_results=command_results,
                blockers=[_command_blocker("release_merge", result)],
                next_actions=["Resolve the merge blocker, then rerun release_workflow(action='merge-dev')."],
            )

    return _tool_output(
        "release_workflow",
        input_summary,
        summary=f"Merged {WORK_BRANCH} into {RELEASE_BRANCH} for release.",
        command_results=command_results,
        next_actions=["Run release_workflow(action='status') before publishing."],
    )


def _record_release_check_success(
    root: Path,
    fingerprint: str,
    command_results: list[dict[str, Any]],
) -> None:
    _record_tree_check_success(root, RELEASE_CHECK_STATE_FILE, fingerprint, command_results)


def _verify_release_check_success(root: Path) -> dict[str, Any]:
    return _verify_tree_check_success(
        root,
        RELEASE_CHECK_STATE_FILE,
        missing_message="No successful release check record exists.",
        mismatch_message="Recorded release check fingerprint does not match the current tree.",
        success_message="Release check record matches the current tree.",
    )


def _record_tree_check_success(
    root: Path,
    state_file: Path,
    fingerprint: str,
    command_results: list[dict[str, Any]],
) -> None:
    state_path = root / state_file
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fingerprint": fingerprint,
        "recorded_at": date.today().isoformat(),
        "commands": [
            {
                "command": result["command"],
                "returncode": result["returncode"],
                "summary": result["summary"],
            }
            for result in command_results
        ],
    }
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _verify_tree_check_success(
    root: Path,
    state_file: Path,
    *,
    missing_message: str,
    mismatch_message: str,
    success_message: str,
) -> dict[str, Any]:
    state_path = root / state_file
    if not state_path.is_file():
        return {"ok": False, "message": missing_message}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "message": f"Check record is invalid: {exc}"}
    try:
        current = _working_tree_fingerprint(root)
    except _FingerprintError as exc:
        return {
            "ok": False,
            "message": str(exc),
            "fingerprint_error": exc.to_dict(),
        }
    recorded = state.get("fingerprint")
    if recorded != current:
        return {
            "ok": False,
            "message": mismatch_message,
            "recorded_fingerprint": recorded,
            "current_fingerprint": current,
        }
    return {"ok": True, "message": success_message, "fingerprint": current}


def _release_readiness(root: Path, *, require_release_check: bool = False) -> dict[str, Any]:
    health = repo_health(root=str(root))
    metadata = metadata_status(root=str(root))
    dependencies = dependency_metadata_status(root=str(root))
    route = route_agent_context("release publish", module=None)
    blockers = [*metadata["blockers"], *dependencies["blockers"]]
    if health["branch"] != RELEASE_BRANCH:
        blockers.append({"phase": "release", "message": f"Release publishing must run from {RELEASE_BRANCH}."})
    if health["dirty"]:
        blockers.append({"phase": "release", "message": "Release publishing requires a clean working tree."})
    if "agent_docs/release.md" not in route["required_files"]:
        blockers.append({"phase": "release", "message": "Release instructions were not routed."})
    remote = _remote_main_status(root, require_equal=True)
    blockers.extend(remote["blockers"])
    remote_dev = _remote_dev_status(root, require_equal=False)
    blockers.extend(remote_dev["blockers"])
    precommit = _verify_precommit_success(root)
    if not precommit["ok"]:
        blockers.append({"phase": "precommit", "message": precommit["message"]})
    release_check = _verify_release_check_success(root) if require_release_check else None
    if release_check is not None and not release_check["ok"]:
        blockers.append({"phase": "release_checks", "message": release_check["message"]})
    return {
        "repo_health": health,
        "metadata_status": metadata,
        "dependency_metadata_status": dependencies,
        "required_instruction_files": route["required_files"],
        "remote_main_status": remote["result"],
        "remote_dev_status": remote_dev["result"],
        "precommit_verification": precommit,
        "release_check_verification": release_check,
        "command_results": [*remote["command_results"], *remote_dev["command_results"]],
        "blockers": blockers,
    }


if __name__ == "__main__":
    raise SystemExit(main())
