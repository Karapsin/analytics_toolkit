#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

docs_assistant = importlib.import_module(
    f"{__package__}.docs_assistant" if __package__ else "docs_assistant"
)
sql_explorer_visual = importlib.import_module(
    f"{__package__}.sql_explorer_visual" if __package__ else "sql_explorer_visual"
)

try:  # pragma: no cover - exercised only when the agent-only MCP dependency exists.
    from mcp.server import MCPServer
except ImportError:  # pragma: no cover - normal package test env has no MCP dependency.
    MCPServer = None  # type: ignore[assignment,misc]


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from release_routines.lib.check_readme_dependencies import validate_readme_dependencies
from release_routines.lib.project_metadata import load_project

DEFAULT_INDEX_DIR = docs_assistant.DEFAULT_INDEX_DIR
CHECK_STATE_FILE = Path(DEFAULT_INDEX_DIR) / "precommit_check.json"
PRECOMMIT_STAGE_STATE_FILE = Path(DEFAULT_INDEX_DIR) / "precommit_stages.json"
RELEASE_CHECK_STATE_FILE = Path(DEFAULT_INDEX_DIR) / "release_check.json"
TOOL_LOG_DIR = Path(DEFAULT_INDEX_DIR) / "tool_logs"
GITHUB_WATCH_DIR = Path(DEFAULT_INDEX_DIR) / "github_checks"
STARTUP_CONTEXT_FILE = Path(DEFAULT_INDEX_DIR) / "startup_context.json"
TOOL_METRICS_FILE = Path(DEFAULT_INDEX_DIR) / "tool_metrics.jsonl"
CHECK_FAILURE_STATE_FILE = Path(DEFAULT_INDEX_DIR) / "check_failure.json"
PRECOMMIT_STAGE_TTL_SECONDS = 24 * 60 * 60
ENV_STATE_FILE = Path(".venv") / ".agent_env_state.json"
DETAIL_LEVELS = ("summary", "diagnostic", "full")
DIAGNOSTIC_EXCERPT_CHARS = 2000
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
INTEGRATION_PROFILES = ("core", "auth", "all", "fault", "stress")
INTEGRATION_CLICKHOUSE_DRIVERS = ("http", "native", "both")
REQUIRED_WORKFLOWS_PATH = Path(".github/required-workflows.json")
GITHUB_CHECK_TIMEOUT_SECONDS = 60 * 60
GITHUB_CHECK_POLL_SECONDS = 15
GITHUB_CHECK_DISCOVERY_SECONDS = 5 * 60
GITHUB_CHECK_WAIT_SECONDS = 60
SQL_ARCHITECTURE_MAX_LINES = 900
SQL_ARCHITECTURE_WARNING_LINES = 50
MCP_SERVER_INSTRUCTIONS = (
    "Repository-local analytics-toolkit workflow server. Call prepare_start before repository "
    "inspection or changes, follow its instruction routing, use change_impact for implementation "
    "preflight, and use the managed status, version, check, git, and release workflows instead of "
    "their underlying scripts."
)
PYTHON_CACHE_DIR = "/tmp/utils_dev_pycache"  # noqa: S108 - repository-wide test cache.
SQL_ARCHITECTURE_EXCEPTIONS = {
    "analytics_toolkit/sql/connection/config.py",
    "analytics_toolkit/sql/dml/load/load_df.py",
}
VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', flags=re.MULTILINE)
PYTHON_REQUIRES_RE = re.compile(r'^requires-python\s*=\s*"([^"]+)"', flags=re.MULTILINE)
README_VERSION_RE = re.compile(r"\*\*Version:\*\*\s+`([^`]+)`")
CHANGELOG_HEADING_RE = re.compile(
    r"^##\s+([0-9]+(?:\.[0-9]+){3})\s+-\s+(.+?)\s*$", flags=re.MULTILINE
)
UNRELEASED_HEADING_RE = re.compile(r"^##\s+Unreleased\s*$", flags=re.IGNORECASE | re.MULTILINE)
DEPENDENCY_RE = re.compile(r'"([^"]+)"')

MODULE_DOCS = {
    "ab_utils": "agent_docs/ab_utils.md",
    "ab": "agent_docs/ab_utils.md",
    "agent_tool": "agent_tools/README.md",
    "agent_tools": "agent_tools/README.md",
    "sql": "agent_docs/sql.md",
    "sql_explorer": "agent_docs/sql.md",
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
            "display": "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/ab_utils",
            "args": ["pytest", "-q", "tests/ab_utils"],
            "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
        },
    ],
    "dates": [
        {
            "display": (
                "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/dates tests/datetime"
            ),
            "args": ["pytest", "-q", "tests/dates", "tests/datetime"],
            "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
        },
    ],
    "excel": [
        {
            "display": "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/excel",
            "args": ["pytest", "-q", "tests/excel"],
            "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
        },
    ],
    "general": [
        {
            "display": "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/general",
            "args": ["pytest", "-q", "tests/general"],
            "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
        },
    ],
    "sql": [
        {
            "display": "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/sql",
            "args": ["pytest", "-q", "tests/sql"],
            "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
        },
    ],
    "agent_tools": [
        {
            "display": (
                "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q "
                "tests/agent_tools tests/repository"
            ),
            "args": ["pytest", "-q", "tests/agent_tools", "tests/repository"],
            "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
        },
    ],
    "mcp": [
        {
            "display": "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/agent_tools/mcp",
            "args": ["pytest", "-q", "tests/agent_tools/mcp"],
            "env": {"PYTHONPYCACHEPREFIX": "/tmp/utils_dev_pycache"},
        },
    ],
}

PRECOMMIT_COMMAND = {
    "display": "release_routines/pre_commit_checks.sh",
    "args": ["release_routines/pre_commit_checks.sh"],
    "env": {},
}

PRECOMMIT_CHECK_COMMANDS = [
    {
        "stage": "static",
        "display": "release_routines/pre_commit_checks.sh --static",
        "args": ["release_routines/pre_commit_checks.sh", "--static"],
        "env": {},
    },
    {
        "stage": "coverage",
        "display": "release_routines/pre_commit_checks.sh --coverage",
        "args": ["release_routines/pre_commit_checks.sh", "--coverage"],
        "env": {},
    },
    {
        "stage": "artifacts",
        "display": "release_routines/pre_commit_checks.sh --artifacts",
        "args": ["release_routines/pre_commit_checks.sh", "--artifacts"],
        "env": {},
    },
    {
        "stage": "matrix",
        "display": "release_routines/pre_commit_checks.sh --matrix",
        "args": ["release_routines/pre_commit_checks.sh", "--matrix"],
        "env": {},
    },
]

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
    {
        "display": (
            "python -m release_routines.sql_integration --profile all --clickhouse-driver both"
        ),
        "args": [
            sys.executable,
            "-m",
            "release_routines.sql_integration",
            "--profile",
            "all",
            "--clickhouse-driver",
            "both",
        ],
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


def prepare_start(  # noqa: PLR0913 - public MCP input shape is intentionally explicit.
    task: str,
    module: str | None = None,
    root: str = ".",
    index_dir: str = DEFAULT_INDEX_DIR,
    ensure_project_env: bool = True,
    detail: str = "summary",
) -> dict[str, Any]:
    """Run the mandatory startup workflow for coding agents."""
    root_path = _resolve_root(root)
    input_summary = {
        "task": task,
        "module": module,
        "root": str(root_path),
        "index_dir": index_dir,
        "ensure_project_env": ensure_project_env,
        "detail": detail,
    }
    if detail not in DETAIL_LEVELS:
        return _tool_output(
            "prepare_start",
            input_summary,
            ok=False,
            summary="Unsupported output detail.",
            blockers=[{"phase": "validate", "message": _detail_error(detail)}],
        )
    command_results: list[dict[str, Any]] = []

    for phase, command in _prepare_sync_commands(root_path):
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

    environment_fingerprint = _environment_fingerprint(root_path, ensure_project_env)
    environment_reused, health_result = _environment_ready(
        root_path,
        ensure_project_env=ensure_project_env,
        fingerprint=environment_fingerprint,
    )
    if health_result is not None:
        command_results.append(health_result)
    if not environment_reused:
        for phase, command in _environment_commands(root_path, ensure_project_env):
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
        _write_environment_state(
            root_path,
            fingerprint=environment_fingerprint,
            ensure_project_env=ensure_project_env,
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
            next_actions=[
                f"Switch to {WORK_BRANCH}, resolve checkout issues, then rerun prepare_start."
            ],
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

    status = workflow_status(
        task=task,
        module=module,
        root=str(root_path),
        detail="diagnostic",
    )
    status_result = status["result"]
    context = _write_startup_context(
        root_path,
        task=task,
        module=module,
        required_files=status_result["required_instruction_files"],
        repository_state={
            "repo_health": status_result["repo_health"],
            "metadata_status": status_result["metadata_status"],
        },
    )
    return _tool_output(
        "prepare_start",
        input_summary,
        summary="Startup workflow completed.",
        result={
            "phase": "complete",
            "repo_health": _compact_repo_health(status_result["repo_health"]),
            "required_instruction_files": status_result["required_instruction_files"],
            "instruction_routing": {
                "auto_discovered": ["AGENTS.md"],
                "read_next": [
                    path
                    for path in status_result["required_instruction_files"]
                    if path != "AGENTS.md"
                ],
            },
            "docs_index": {
                "file_count": index.file_count,
                "chunk_count": index.chunk_count,
            },
            "environment": {
                "reused": environment_reused,
            },
            "startup_context": {"id": context["id"]},
        },
        command_results=command_results,
        next_actions=[
            "Read instruction_routing.read_next; do not reread auto-discovered AGENTS.md.",
            "Call change_impact(...) for consolidated RAG and implementation preflight context.",
            "Call workflow_status(...) before and after changes.",
        ],
    )


def docs(
    query: str,
    mode: str = "search",
    top_k: int = 3,
    index_dir: str = DEFAULT_INDEX_DIR,
    detail: str = "summary",
) -> dict[str, Any]:
    """Search or answer from the local docs RAG index."""
    resolved_index_dir = _resolve_index_dir(index_dir)
    input_summary = {
        "query": query,
        "mode": mode,
        "top_k": top_k,
        "index_dir": index_dir,
        "resolved_index_dir": str(resolved_index_dir),
        "detail": detail,
    }
    if detail not in DETAIL_LEVELS:
        return _tool_output(
            "docs",
            input_summary,
            ok=False,
            summary="Unsupported output detail.",
            blockers=[{"phase": "validate", "message": _detail_error(detail)}],
        )
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
            candidates = docs_assistant.search_docs(
                query,
                index_dir=resolved_index_dir,
                top_k=max(top_k * 3, top_k),
            )
            results = _dedupe_search_results(candidates)[:top_k]
            result: dict[str, Any] = {
                "mode": mode,
                "results": [_search_result_to_dict(item, detail=detail) for item in results],
                "returned_count": len(results),
                "total_count": len(candidates),
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
                "answer": _bounded_text(answer.answer, 1200),
                "citations": _unique(answer.citations)[:top_k],
                "returned_count": min(len(answer.citations), top_k),
                "total_count": len(answer.citations),
                "freshness_warnings": _freshness_warnings(resolved_index_dir),
            }
            if detail != "summary":
                result["results"] = [
                    _search_result_to_dict(item, detail=detail) for item in answer.results
                ]
    except Exception as exc:
        return _tool_output(
            "docs",
            input_summary,
            ok=False,
            summary="Docs retrieval failed.",
            blockers=[{"phase": "docs", "message": str(exc)}],
            next_actions=[
                "Run prepare_start(...) to rebuild the docs index, then retry docs(...)."
            ],
        )

    return _tool_output(
        "docs",
        input_summary,
        summary=f"Docs {mode} completed.",
        result=result,
        next_actions=["Use cited files as focused context before repository inspection."],
    )


def workflow_metrics(
    session_id: str | None = None,
    root: str = ".",
    detail: str = "summary",
) -> dict[str, Any]:
    """Summarize persisted MCP response and retry costs for one startup session."""
    root_path = _resolve_root(root)
    input_summary = {"session_id": session_id, "root": str(root_path), "detail": detail}
    if detail not in DETAIL_LEVELS:
        return _tool_output(
            "workflow_metrics",
            input_summary,
            ok=False,
            summary="Unsupported output detail.",
            blockers=[{"phase": "validate", "message": _detail_error(detail)}],
        )
    active = _read_startup_context(root_path)
    selected = session_id or str(active.get("id", ""))
    entries = _read_tool_metrics(root_path, selected)
    by_tool: dict[str, dict[str, int]] = {}
    failure_counts: dict[str, int] = {}
    for entry in entries:
        bucket = by_tool.setdefault(
            str(entry.get("tool", "unknown")),
            {"calls": 0, "response_bytes": 0, "raw_output_bytes": 0, "failures": 0},
        )
        bucket["calls"] += 1
        bucket["response_bytes"] += int(entry.get("response_bytes", 0))
        bucket["raw_output_bytes"] += int(entry.get("raw_output_bytes", 0))
        if not entry.get("ok", False):
            bucket["failures"] += 1
        signature = entry.get("failure_signature")
        if isinstance(signature, str) and signature:
            failure_counts[signature] = failure_counts.get(signature, 0) + 1
    response_bytes = sum(item["response_bytes"] for item in by_tool.values())
    repeated_failures = sum(count - 1 for count in failure_counts.values() if count > 1)
    result: dict[str, Any] = {
        "session_id": selected or None,
        "call_count": len(entries),
        "response_bytes": response_bytes,
        "estimated_response_tokens": math.ceil(response_bytes / 4),
        "token_estimate_method": "ceil(serialized_response_bytes / 4); model tokens unavailable",
        "repeated_failure_count": repeated_failures,
        "by_tool": by_tool,
    }
    if detail != "summary":
        result["entries"] = entries
    return _tool_output(
        "workflow_metrics",
        input_summary,
        summary="Workflow metrics collected.",
        result=result,
    )


def workflow_status(  # noqa: PLR0913 - public MCP input shape is intentionally explicit.
    task: str,
    module: str | None = None,
    change_type: str = "implementation",
    instructions_read: bool = False,
    root: str = ".",
    detail: str = "summary",
) -> dict[str, Any]:
    """Return route, repository, metadata, and check status for the workflow."""
    root_path = _resolve_root(root)
    input_summary = {
        "task": task,
        "module": module,
        "change_type": change_type,
        "instructions_read": instructions_read,
        "root": str(root_path),
        "detail": detail,
    }
    if detail not in DETAIL_LEVELS:
        return _tool_output(
            "workflow_status",
            input_summary,
            ok=False,
            summary="Unsupported output detail.",
            blockers=[{"phase": "validate", "message": _detail_error(detail)}],
        )
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
    full_result = {
        "repo_health": health,
        "required_instruction_files": route["required_files"],
        "routing": route,
        "metadata_status": metadata,
        "dependency_metadata_status": dependency_metadata,
        "recommended_checks": recommended,
        "missing_mandatory_actions": missing,
        "sql_explorer_visual_review": sql_explorer_visual.verify_visual_receipt(root_path),
    }
    context = _workflow_context(
        root_path,
        task=task,
        module=module,
        repository_state={"repo_health": health, "metadata_status": metadata},
        update=True,
    )
    result = (
        full_result
        if detail != "summary"
        else {
            "repo_health": _compact_repo_health(health),
            "required_instruction_files": (
                [] if instructions_read and context["reused"] else route["required_files"]
            ),
            "check_plan": {
                "area": recommended["area"],
                "focused_count": len(recommended["focused_commands"]),
                "required_final_count": len(recommended["required_final_commands"]),
            },
            "missing_mandatory_actions": missing,
            "startup_context": context,
        }
    )
    if detail != "summary":
        result["startup_context"] = context
    return _tool_output(
        "workflow_status",
        input_summary,
        ok=ok,
        summary="Workflow status collected." if ok else "Workflow status requires action.",
        result=result,
        blockers=[*metadata["blockers"], *dependency_metadata["blockers"]],
        next_actions=_workflow_next_actions(missing),
    )


def change_impact(
    task: str,
    module: str | None = None,
    symbols: list[str] | None = None,
    paths: list[str] | None = None,
    root: str = ".",
) -> dict[str, Any]:
    """Return read-only implementation constraints before repository changes."""
    root_path = _resolve_root(root)
    requested_symbols = _unique(symbols or [])
    requested_paths = _unique(paths or [])
    input_summary = {
        "task": task,
        "module": module,
        "symbols": requested_symbols,
        "paths": requested_paths,
        "root": str(root_path),
    }
    route = route_agent_context(task=f"implementation {task}", module=module)
    contracts, inferred_paths, contract_blockers = _public_contract_impact(
        root_path,
        module=module,
        symbols=requested_symbols,
    )
    architecture = _architecture_impact(
        root_path,
        module=module,
        paths=[*requested_paths, *inferred_paths],
    )
    changelog_text = _read_text(root_path / CHANGELOG_PATH)
    unreleased_count = len(_unreleased_changelog_bullets(changelog_text))
    docs_response = docs(
        task,
        mode="search",
        top_k=3,
        index_dir=str(root_path / DEFAULT_INDEX_DIR),
    )
    references = []
    if docs_response["ok"]:
        references = [
            {
                "citation": item["citation"],
                "heading": item["heading"],
                "snippet": _bounded_text(item["snippet"], 500),
            }
            for item in docs_response["result"].get("results", [])
        ]
    documentation = _change_documentation_paths(root_path, module, requested_symbols)
    return _tool_output(
        "change_impact",
        input_summary,
        ok=not contract_blockers,
        summary=(
            "Change impact collected."
            if not contract_blockers
            else "Change impact collected with unresolved public symbols."
        ),
        result={
            "required_instruction_files": route["required_files"],
            "rag_references": references,
            "public_contracts": contracts,
            "architecture": architecture,
            "documentation_paths": documentation,
            "changelog": {
                "unreleased_count": unreleased_count,
                "next_action": (
                    "roll_unreleased_into_new_version"
                    if unreleased_count + 1 >= UNRELEASED_CHANGELOG_THRESHOLD
                    else "add_unreleased_entry"
                ),
            },
            "recommended_checks": recommend_tests(
                area=module or task,
                change_type="implementation",
            ),
        },
        blockers=contract_blockers,
        next_actions=[
            "Use the reported contract pointers, line budgets, documentation paths, and checks in the implementation plan."
        ],
    )


def _public_contract_impact(
    root: Path,
    *,
    module: str | None,
    symbols: list[str],
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    if not symbols:
        return [], [], []
    module_name = _normalize_area(module or "")
    if module_name != "sql" and any(symbol.startswith("sql.") for symbol in symbols):
        module_name = "sql"
    if module_name != "sql":
        return (
            [],
            [],
            [
                {
                    "phase": "change_impact",
                    "message": "public contract inspection currently supports module='sql'",
                }
            ],
        )
    public_module = importlib.import_module("analytics_toolkit.sql")
    manifest_path = root / "integration" / "sql_coverage_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        exports = manifest["exports"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as exc:
        return [], [], [{"phase": "sql_manifest", "message": str(exc)}]
    contracts: list[dict[str, Any]] = []
    inferred_paths: list[str] = []
    blockers: list[dict[str, Any]] = []
    for requested in symbols:
        name = requested.rsplit(".", 1)[-1]
        obj = getattr(public_module, name, None)
        if obj is None:
            blockers.append(
                {
                    "phase": "public_symbol",
                    "message": f"analytics_toolkit.sql has no public symbol {name!r}",
                }
            )
            continue
        if not inspect.isfunction(obj):
            contracts.append(
                {
                    "symbol": f"sql.{name}",
                    "signature": None,
                    "manifest_pointer": f"/exports/{name}",
                    "classification": exports.get(name, {}).get("classification"),
                }
            )
            continue
        signature = inspect.signature(obj)
        declared_parameters = exports.get(name, {}).get("parameters", {})
        parameters: list[dict[str, Any]] = []
        for parameter_name, parameter in signature.parameters.items():
            expected_default = (
                "<required>"
                if parameter.default is inspect.Parameter.empty
                else repr(parameter.default)
            )
            declared = declared_parameters.get(parameter_name)
            parameters.append(
                {
                    "name": parameter_name,
                    "pointer": f"/exports/{name}/parameters/{parameter_name}",
                    "signature_default": expected_default,
                    "kind": str(parameter.kind),
                    "manifest_status": (
                        "missing"
                        if declared is None
                        else "mismatch"
                        if declared.get("signature_default") != expected_default
                        or declared.get("kind") != str(parameter.kind)
                        else "aligned"
                    ),
                    "missing_entry_template": (
                        {
                            "signature_default": expected_default,
                            "kind": str(parameter.kind),
                            "states": [
                                {
                                    "status": "covered",
                                    "tests": ["<add exact test node id>"],
                                }
                            ],
                        }
                        if declared is None
                        else None
                    ),
                }
            )
        source = inspect.getsourcefile(obj)
        if source:
            try:
                relative_source = Path(source).resolve().relative_to(root).as_posix()
            except ValueError:
                relative_source = None
            if relative_source is not None:
                inferred_paths.append(relative_source)
        contracts.append(
            {
                "symbol": f"sql.{name}",
                "signature": str(signature),
                "manifest_pointer": f"/exports/{name}",
                "parameters": parameters,
                "extra_manifest_parameters": sorted(
                    set(declared_parameters) - set(signature.parameters)
                ),
            }
        )
    return contracts, _unique(inferred_paths), blockers


def _architecture_impact(
    root: Path,
    *,
    module: str | None,
    paths: list[str],
) -> dict[str, Any]:
    normalized = {
        path for path in paths if path.startswith("analytics_toolkit/sql/") and path.endswith(".py")
    }
    if _normalize_area(module or "") == "sql":
        for path in (root / "analytics_toolkit" / "sql").rglob("*.py"):
            rel_path = path.relative_to(root).as_posix()
            if rel_path in SQL_ARCHITECTURE_EXCEPTIONS:
                continue
            line_count = len(path.read_text(encoding="utf-8").splitlines())
            if line_count >= SQL_ARCHITECTURE_MAX_LINES - SQL_ARCHITECTURE_WARNING_LINES:
                normalized.add(rel_path)
    modules = []
    for rel_path in sorted(normalized):
        path = root / rel_path
        if not path.is_file():
            modules.append({"path": rel_path, "status": "missing"})
            continue
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        excepted = rel_path in SQL_ARCHITECTURE_EXCEPTIONS
        remaining = None if excepted else SQL_ARCHITECTURE_MAX_LINES - line_count
        modules.append(
            {
                "path": rel_path,
                "line_count": line_count,
                "max_lines": None if excepted else SQL_ARCHITECTURE_MAX_LINES,
                "remaining_lines": remaining,
                "status": (
                    "excepted"
                    if excepted
                    else "over_limit"
                    if remaining is not None and remaining < 0
                    else "at_limit"
                    if remaining == 0
                    else "low_headroom"
                    if remaining is not None and remaining <= SQL_ARCHITECTURE_WARNING_LINES
                    else "within_limit"
                ),
            }
        )
    return {"policy": "SQL modules must not exceed 900 lines", "modules": modules}


def _change_documentation_paths(
    root: Path,
    module: str | None,
    symbols: list[str],
) -> list[str]:
    paths = [CHANGELOG_PATH]
    if _normalize_area(module or "") == "sql":
        for symbol in symbols:
            name = symbol.rsplit(".", 1)[-1]
            candidate = f"docs/modules/sql/functions/{name}.md"
            if (root / candidate).is_file():
                paths.append(candidate)
    return _unique(paths)


def _version_bump_validation_error(
    summary: str | None,
    change_type: str,
    force_release: bool,  # noqa: FBT001 - this mirrors the public named option.
) -> tuple[str, str] | None:
    if force_release and not _is_release_artifact(change_type):
        return (
            "Forced version bump is restricted to release workflows.",
            "force_release requires a release-oriented change_type",
        )
    if force_release and summary is not None:
        return (
            "Forced version bump does not accept a changelog summary.",
            "omit summary when force_release is enabled",
        )
    if not force_release and summary is None:
        return "Version bump requires a changelog summary.", "summary is required"
    return None


def version_bump(  # noqa: C901 - this function coordinates the atomic metadata workflow.
    summary: str | None = None,
    change_type: str = "implementation",
    dry_run: bool = False,  # noqa: FBT001, FBT002 - named MCP/CLI option.
    force_release: bool = False,  # noqa: FBT001, FBT002 - named MCP/CLI option.
    root: str = ".",
) -> dict[str, Any]:
    """Plan or apply the repository version bump and changelog update."""
    root_path = _resolve_root(root)
    input_summary = {
        "summary": summary,
        "change_type": change_type,
        "dry_run": dry_run,
        "force_release": force_release,
        "root": str(root_path),
    }
    validation_error = _version_bump_validation_error(summary, change_type, force_release)
    if validation_error is not None:
        summary_text, message = validation_error
        return _tool_output(
            "version_bump",
            input_summary,
            ok=False,
            summary=summary_text,
            blockers=[{"phase": "validate", "message": message}],
        )
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
    unreleased_bullets = _unreleased_changelog_bullets(changelog_text)
    if force_release and not unreleased_bullets:
        return _tool_output(
            "version_bump",
            input_summary,
            ok=False,
            summary="Forced version bump requires unreleased changelog entries.",
            blockers=[
                {
                    "phase": "validate",
                    "message": "no unreleased changelog entries are available to release",
                }
            ],
        )
    bullet = "" if force_release else _format_changelog_bullet(summary or "")
    planned_unreleased_count = len(unreleased_bullets) + (0 if force_release else 1)
    next_version_value = _increment_version(current_version)
    should_bump = force_release or planned_unreleased_count >= UNRELEASED_CHANGELOG_THRESHOLD
    if should_bump:
        release_bullets = unreleased_bullets if force_release else [*unreleased_bullets, bullet]
        entry = _format_changelog_entry(
            next_version_value,
            release_bullets,
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


def run_checks(  # noqa: PLR0913 - public MCP input shape is intentionally explicit.
    area: str | None = None,
    change_type: str = "implementation",
    level: str = "focused",
    dry_run: bool = False,
    integration_profile: str = "all",
    integration_clickhouse_driver: str = "both",
    root: str = ".",
    detail: str = "summary",
) -> dict[str, Any]:
    """Plan or execute focused, integration, pre-commit, or release checks."""
    root_path = _resolve_root(root)
    input_summary = {
        "area": area,
        "change_type": change_type,
        "level": level,
        "dry_run": dry_run,
        "integration_profile": integration_profile,
        "integration_clickhouse_driver": integration_clickhouse_driver,
        "root": str(root_path),
        "detail": detail,
    }
    if detail not in DETAIL_LEVELS:
        return _tool_output(
            "run_checks",
            input_summary,
            ok=False,
            summary="Unsupported output detail.",
            blockers=[{"phase": "validate", "message": _detail_error(detail)}],
        )
    try:
        commands = _check_commands(
            area=area,
            change_type=change_type,
            level=level,
            integration_profile=integration_profile,
            integration_clickhouse_driver=integration_clickhouse_driver,
            root=root_path,
        )
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

    execution = _execute_check_commands(root_path, commands, level)
    command_results = execution["command_results"]
    stage_reports = execution["stage_reports"]
    fingerprint_error = execution.get("fingerprint_error")
    if fingerprint_error:
        return _tool_output(
            "run_checks",
            input_summary,
            ok=False,
            summary="Could not record an exact-tree pre-commit stage receipt.",
            result={"stages": stage_reports},
            command_results=command_results,
            blockers=[fingerprint_error.to_dict()],
            next_actions=["Fix the git fingerprinting failure, then rerun pre-commit checks."],
        )
    failed_result = execution.get("failed_result")
    if failed_result:
        blocker = _check_failure_blocker(failed_result)
        blocker, failure = _annotate_check_failure(root_path, blocker)
        return _tool_output(
            "run_checks",
            input_summary,
            ok=False,
            summary="A validation command failed.",
            result={
                "level": level,
                "command_count": len(commands),
                "failed_command_index": execution["failed_command_index"],
                "executed_stage_count": len(command_results),
                "reused_stage_count": _reused_stage_count(stage_reports),
                "stages": stage_reports,
                **failure,
            },
            command_results=command_results,
            blockers=[blocker],
            next_actions=[_check_next_action(blocker, level)],
        )

    result_data: dict[str, Any] = {"level": level, "command_count": len(commands)}
    if level == "precommit":
        result_data.update(
            {
                "executed_stage_count": len(command_results),
                "reused_stage_count": _reused_stage_count(stage_reports),
                "stages": stage_reports,
            }
        )
    coverage_changes = _managed_coverage_changes(command_results)
    if coverage_changes:
        result_data["coverage_target_changes"] = coverage_changes
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
                next_actions=[
                    "Fix the git fingerprinting failure, then rerun run_checks(level='precommit')."
                ],
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


def _execute_check_commands(
    root: Path,
    commands: list[dict[str, Any]],
    level: str,
) -> dict[str, Any]:
    command_results: list[dict[str, Any]] = []
    stage_reports: list[dict[str, Any]] = []
    context, fingerprint_error = _precommit_execution_context(root, level)
    if fingerprint_error:
        return {
            "command_results": command_results,
            "stage_reports": stage_reports,
            "fingerprint_error": fingerprint_error,
        }

    for command_index, command in enumerate(commands):
        stage = str(command.get("stage") or f"command-{command_index + 1}")
        receipt = _reusable_precommit_stage(context, stage, command)
        if receipt:
            stage_reports.append(
                {
                    "stage": stage,
                    "status": "reused",
                    "elapsed_seconds": 0.0,
                    "previous_elapsed_seconds": receipt.get("elapsed_seconds", 0.0),
                }
            )
            continue

        stage_started = time.monotonic()
        result = _run_command(root, command)
        stage_elapsed = round(time.monotonic() - stage_started, 3)
        command_results.append(result)
        status = "executed" if result["ok"] else "failed"
        stage_reports.append({"stage": stage, "status": status, "elapsed_seconds": stage_elapsed})
        if not result["ok"]:
            return {
                "command_results": command_results,
                "stage_reports": stage_reports,
                "failed_result": result,
                "failed_command_index": command_index,
            }
        if context is not None:
            fingerprint, fingerprint_error = _try_working_tree_fingerprint(root)
            if fingerprint_error:
                return {
                    "command_results": command_results,
                    "stage_reports": stage_reports,
                    "fingerprint_error": fingerprint_error,
                }
            context["fingerprint"] = fingerprint
            receipt = {
                "fingerprint": fingerprint,
                "toolchain_fingerprint": context["toolchain_fingerprint"],
                "command_fingerprint": _precommit_command_fingerprint(command),
                "completed_at": time.time(),
                "elapsed_seconds": stage_elapsed,
            }
            _record_precommit_stage_success(root, stage, receipt)
            context["state"] = _load_precommit_stage_state(root)
    return {"command_results": command_results, "stage_reports": stage_reports}


def _precommit_execution_context(
    root: Path,
    level: str,
) -> tuple[dict[str, Any] | None, _FingerprintError | None]:
    if level != "precommit":
        return None, None
    fingerprint, error = _try_working_tree_fingerprint(root)
    if error:
        return None, error
    return {
        "fingerprint": fingerprint,
        "toolchain_fingerprint": _precommit_toolchain_fingerprint(root),
        "state": _load_precommit_stage_state(root),
    }, None


def _try_working_tree_fingerprint(
    root: Path,
) -> tuple[str | None, _FingerprintError | None]:
    try:
        return _working_tree_fingerprint(root), None
    except _FingerprintError as exc:
        return None, exc


def _reusable_precommit_stage(
    context: dict[str, Any] | None,
    stage: str,
    command: dict[str, Any],
) -> dict[str, Any] | None:
    if context is None:
        return None
    receipt = context["state"].get("stages", {}).get(stage)
    if _precommit_stage_receipt_is_current(
        receipt,
        fingerprint=context["fingerprint"],
        toolchain_fingerprint=context["toolchain_fingerprint"],
        command=command,
    ):
        return receipt
    return None


def _reused_stage_count(stage_reports: list[dict[str, Any]]) -> int:
    return sum(report["status"] == "reused" for report in stage_reports)


def git_workflow(  # noqa: C901, PLR0911, PLR0912, PLR0913 - workflow coordinator.
    action: str,
    message: str | None = None,
    paths: list[str] | None = None,
    sha: str | None = None,
    check_timeout_seconds: int = GITHUB_CHECK_TIMEOUT_SECONDS,
    wait_seconds: int = GITHUB_CHECK_WAIT_SECONDS,
    root: str = ".",
    detail: str = "summary",
) -> dict[str, Any]:
    """Run repository git workflow actions with structured blockers."""
    root_path = _resolve_root(root)
    input_summary = {
        "action": action,
        "message": message,
        "paths": paths,
        "sha": sha,
        "check_timeout_seconds": check_timeout_seconds,
        "wait_seconds": wait_seconds,
        "root": str(root_path),
        "detail": detail,
    }
    if detail not in DETAIL_LEVELS:
        return _tool_output(
            "git_workflow",
            input_summary,
            ok=False,
            summary="Unsupported output detail.",
            blockers=[{"phase": "validate", "message": _detail_error(detail)}],
        )
    if wait_seconds <= 0:
        return _tool_output(
            "git_workflow",
            input_summary,
            ok=False,
            summary="Invalid GitHub check wait interval.",
            blockers=[{"phase": "validate", "message": "wait_seconds must be positive"}],
        )
    if action not in {"checks", "commit", "push"}:
        return _tool_output(
            "git_workflow",
            input_summary,
            ok=False,
            summary="Unsupported git workflow action.",
            blockers=[
                {
                    "phase": "validate",
                    "message": "action must be 'checks', 'commit', or 'push'",
                }
            ],
        )

    if action == "checks":
        if not sha:
            return _tool_output(
                "git_workflow",
                input_summary,
                ok=False,
                summary="Commit SHA is required to resume GitHub checks.",
                blockers=[{"phase": "validate", "message": "sha is required for checks"}],
            )
        return _github_checks_workflow(
            root_path,
            input_summary,
            sha=sha,
            timeout_seconds=check_timeout_seconds,
            wait_seconds=wait_seconds,
        )

    if action == "push":
        return _push_dev_workflow(
            root_path,
            input_summary,
            timeout_seconds=check_timeout_seconds,
            wait_seconds=wait_seconds,
        )

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
            next_actions=[
                "Pass explicit paths for the current batch, then retry git_workflow(action='commit')."
            ],
        )
    visual_verification = sql_explorer_visual.verify_visual_receipt(
        root_path,
        paths=commit_paths,
    )
    if not visual_verification["ok"]:
        return _tool_output(
            "git_workflow",
            input_summary,
            ok=False,
            summary="Commit blocked by SQL Explorer visual review.",
            result={"sql_explorer_visual_review": visual_verification},
            blockers=[
                {
                    "phase": "visual_review",
                    "message": visual_verification["message"],
                }
            ],
            next_actions=[
                "Capture every SQL Explorer scene in a fresh macOS VM, inspect every PNG, "
                "record pass verdicts, and complete visual_workflow before committing."
            ],
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
            next_actions=[
                "Run version_bump(...), include all version metadata paths, then retry the commit."
            ],
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

    checks = _watch_pushed_commit(
        root_path,
        sha=push["sha"],
        timeout_seconds=check_timeout_seconds,
        wait_seconds=wait_seconds,
        detail=detail,
    )
    command_results.extend(checks["command_results"])
    if checks["blockers"]:
        return _tool_output(
            "git_workflow",
            input_summary,
            ok=False,
            summary="Commit and push completed, but exact-SHA GitHub verification failed.",
            result={
                "push_readiness": push["readiness"],
                "github_checks": checks["result"],
            },
            command_results=command_results,
            blockers=checks["blockers"],
            next_actions=[
                "Fix deterministic failures or retry only demonstrated infrastructure failures, "
                "then push and watch the new exact SHA."
            ],
        )
    pending = checks["result"].get("status") == "pending"
    return _tool_output(
        "git_workflow",
        input_summary,
        summary=(
            "Commit and push completed; exact-SHA GitHub verification is pending."
            if pending
            else "Commit, push, and exact-SHA GitHub verification completed."
        ),
        result={
            "mutation": {
                "sha": push["sha"],
                "path_count": len(commit_paths),
                "push_target": push["push_target"],
            },
            "github_checks": checks["result"],
        },
        command_results=command_results,
        next_actions=(
            [f"Resume with git_workflow(action='checks', sha='{push['sha']}')."] if pending else []
        ),
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
            blockers=[
                {
                    "phase": "validate",
                    "message": "action must be 'status', 'merge-dev', or 'publish'",
                }
            ],
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
                next_actions=[
                    "Resolve release blockers, then rerun release_workflow(action='status')."
                ],
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
                    next_actions=[
                        "Fix the release validation failure, then rerun release_workflow(action='status')."
                    ],
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
                next_actions=[
                    "Fix the git fingerprinting failure, then rerun release_workflow(action='status')."
                ],
            )
        _record_release_check_success(root_path, fingerprint, command_results)
        status["release_check_verification"] = _verify_release_check_success(root_path)
        return _tool_output(
            "release_workflow",
            input_summary,
            summary="Release is ready.",
            result=status,
            command_results=[*status["command_results"], *command_results],
            next_actions=[
                "Use release_workflow(action='publish') only after all blockers are resolved."
            ],
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
        summary="Release publish workflow completed."
        if result["ok"]
        else "Release publish workflow failed.",
        result=status,
        command_results=[*status["command_results"], result],
        blockers=[] if result["ok"] else [_command_blocker("publish", result)],
    )


def create_mcp_server() -> Any:
    """Create the MCP server instance."""
    if MCPServer is None:
        msg = (
            "The MCP SDK 2 runtime is not installed. Using Python 3.10 or newer, run "
            "`.venv/bin/python -m pip install -r agent_tools/requirements-mcp.txt` "
            "from the repository root, then start this server again."
        )
        raise RuntimeError(msg)

    server = MCPServer(
        "analytics-toolkit-agent-tools",
        instructions=MCP_SERVER_INSTRUCTIONS,
    )

    server.tool()(prepare_start)
    server.tool()(docs)
    server.tool()(workflow_status)
    server.tool()(workflow_metrics)
    server.tool()(change_impact)
    server.tool()(version_bump)
    server.tool()(run_checks)
    server.tool()(sql_explorer_visual.visual_workflow)
    server.tool()(sql_explorer_visual.visual_review)
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
    commands = [_sql_focused_command(REPO_ROOT)] if key == "sql" else TEST_COMMANDS.get(key, [])
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
            "Unit tests must not access real databases.",
            "Use run_checks(area='sql', level='integration') only for the disposable SQL stack.",
            "Use fake connections, monkeypatching, and tests/conftest.py fixtures for unit tests.",
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
                    "tool": args.command.replace("-", "_")
                    if hasattr(args, "command")
                    else "unknown",
                    "error": str(exc),
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 1


def _build_cli_parser() -> argparse.ArgumentParser:  # noqa: PLR0915 - CLI mirrors MCP inputs.
    parser = argparse.ArgumentParser(prog="agent_tools/mcp_tool.sh")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare-start")
    prepare_parser.add_argument("--task", required=True)
    prepare_parser.add_argument("--module")
    prepare_parser.add_argument("--root", default=".")
    prepare_parser.add_argument("--index-dir", default=DEFAULT_INDEX_DIR)
    prepare_parser.add_argument("--detail", choices=DETAIL_LEVELS, default="summary")
    prepare_parser.add_argument(
        "--ensure-project-env", dest="ensure_project_env", action="store_true", default=True
    )
    prepare_parser.add_argument(
        "--no-ensure-project-env", dest="ensure_project_env", action="store_false"
    )
    prepare_parser.set_defaults(
        handler=lambda args: prepare_start(
            task=args.task,
            module=args.module,
            root=args.root,
            index_dir=args.index_dir,
            ensure_project_env=args.ensure_project_env,
            detail=args.detail,
        )
    )

    docs_parser = subparsers.add_parser("docs")
    docs_parser.add_argument("query")
    docs_parser.add_argument("--mode", choices=["search", "ask"], default="search")
    docs_parser.add_argument("--top-k", type=int, default=3)
    docs_parser.add_argument("--index-dir", default=DEFAULT_INDEX_DIR)
    docs_parser.add_argument("--detail", choices=DETAIL_LEVELS, default="summary")
    docs_parser.set_defaults(
        handler=lambda args: docs(
            query=args.query,
            mode=args.mode,
            top_k=args.top_k,
            index_dir=args.index_dir,
            detail=args.detail,
        )
    )

    workflow_parser = subparsers.add_parser("workflow-status")
    workflow_parser.add_argument("--task", required=True)
    workflow_parser.add_argument("--module")
    workflow_parser.add_argument("--change-type", default="implementation")
    workflow_parser.add_argument("--instructions-read", action="store_true")
    workflow_parser.add_argument("--root", default=".")
    workflow_parser.add_argument("--detail", choices=DETAIL_LEVELS, default="summary")
    workflow_parser.set_defaults(
        handler=lambda args: workflow_status(
            task=args.task,
            module=args.module,
            change_type=args.change_type,
            instructions_read=args.instructions_read,
            root=args.root,
            detail=args.detail,
        )
    )

    metrics_parser = subparsers.add_parser("workflow-metrics")
    metrics_parser.add_argument("--session-id")
    metrics_parser.add_argument("--root", default=".")
    metrics_parser.add_argument("--detail", choices=DETAIL_LEVELS, default="summary")
    metrics_parser.set_defaults(
        handler=lambda args: workflow_metrics(
            session_id=args.session_id,
            root=args.root,
            detail=args.detail,
        )
    )

    impact_parser = subparsers.add_parser("change-impact")
    impact_parser.add_argument("--task", required=True)
    impact_parser.add_argument("--module")
    impact_parser.add_argument("--symbol", dest="symbols", action="append")
    impact_parser.add_argument("--path", dest="paths", action="append")
    impact_parser.add_argument("--root", default=".")
    impact_parser.set_defaults(
        handler=lambda args: change_impact(
            task=args.task,
            module=args.module,
            symbols=args.symbols,
            paths=args.paths,
            root=args.root,
        )
    )

    bump_parser = subparsers.add_parser("version-bump")
    bump_parser.add_argument("summary", nargs="?")
    bump_parser.add_argument("--change-type", default="implementation")
    bump_parser.add_argument("--dry-run", action="store_true")
    bump_parser.add_argument("--force-release", action="store_true")
    bump_parser.add_argument("--root", default=".")
    bump_parser.set_defaults(
        handler=lambda args: version_bump(
            summary=args.summary,
            change_type=args.change_type,
            dry_run=args.dry_run,
            force_release=args.force_release,
            root=args.root,
        )
    )

    checks_parser = subparsers.add_parser("run-checks")
    checks_parser.add_argument("--area")
    checks_parser.add_argument("--change-type", default="implementation")
    checks_parser.add_argument(
        "--level",
        choices=["focused", "integration", "precommit", "release"],
        default="focused",
    )
    checks_parser.add_argument("--dry-run", action="store_true")
    checks_parser.add_argument(
        "--integration-profile",
        choices=INTEGRATION_PROFILES,
        default="all",
    )
    checks_parser.add_argument(
        "--integration-clickhouse-driver",
        choices=INTEGRATION_CLICKHOUSE_DRIVERS,
        default="both",
    )
    checks_parser.add_argument("--root", default=".")
    checks_parser.add_argument("--detail", choices=DETAIL_LEVELS, default="summary")
    checks_parser.set_defaults(
        handler=lambda args: run_checks(
            area=args.area,
            change_type=args.change_type,
            level=args.level,
            dry_run=args.dry_run,
            integration_profile=args.integration_profile,
            integration_clickhouse_driver=args.integration_clickhouse_driver,
            root=args.root,
            detail=args.detail,
        )
    )

    visual_workflow_parser = subparsers.add_parser("visual-workflow")
    visual_workflow_parser.add_argument(
        "action", choices=["start", "capture", "status", "complete"]
    )
    visual_workflow_parser.add_argument("--review-id")
    visual_workflow_parser.add_argument("--root", default=".")
    visual_workflow_parser.set_defaults(
        handler=lambda args: sql_explorer_visual.visual_workflow(
            action=args.action,
            review_id=args.review_id,
            root=args.root,
        )
    )

    visual_review_parser = subparsers.add_parser("visual-review")
    visual_review_parser.add_argument("--review-id", required=True)
    visual_review_parser.add_argument("--scene-id", required=True)
    visual_review_parser.add_argument(
        "--verdict",
        choices=["pass", "product_defect", "infrastructure_failure"],
        required=True,
    )
    visual_review_parser.add_argument("--notes")
    visual_review_parser.add_argument("--root", default=".")
    visual_review_parser.set_defaults(
        handler=lambda args: sql_explorer_visual.visual_review(
            review_id=args.review_id,
            scene_id=args.scene_id,
            verdict=args.verdict,
            notes=args.notes,
            root=args.root,
        )
    )

    git_parser = subparsers.add_parser("git-workflow")
    git_parser.add_argument("action", choices=["checks", "commit", "push"])
    git_parser.add_argument("--message")
    git_parser.add_argument("--path", dest="paths", action="append")
    git_parser.add_argument("--sha")
    git_parser.add_argument(
        "--check-timeout-seconds",
        type=int,
        default=GITHUB_CHECK_TIMEOUT_SECONDS,
    )
    git_parser.add_argument(
        "--wait-seconds",
        type=int,
        default=GITHUB_CHECK_WAIT_SECONDS,
    )
    git_parser.add_argument("--root", default=".")
    git_parser.add_argument("--detail", choices=DETAIL_LEVELS, default="summary")
    git_parser.set_defaults(
        handler=lambda args: git_workflow(
            action=args.action,
            message=args.message,
            paths=args.paths,
            sha=args.sha,
            check_timeout_seconds=args.check_timeout_seconds,
            wait_seconds=args.wait_seconds,
            root=args.root,
            detail=args.detail,
        )
    )

    release_parser = subparsers.add_parser("release-workflow")
    release_parser.add_argument(
        "--action", choices=["status", "merge-dev", "publish"], default="status"
    )
    release_parser.add_argument("--root", default=".")
    release_parser.set_defaults(
        handler=lambda args: release_workflow(action=args.action, root=args.root)
    )

    return parser


def _prepare_sync_commands(_root: Path) -> list[tuple[str, dict[str, Any]]]:
    return [
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


def _environment_commands(root: Path, ensure_project_env: bool) -> list[tuple[str, dict[str, Any]]]:
    venv_python = root / ".venv" / "bin" / "python"
    commands: list[tuple[str, dict[str, Any]]] = []
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
                "args": [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--quiet",
                    "--disable-pip-version-check",
                    "-r",
                    "agent_tools/requirements-mcp.txt",
                ],
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
                    "args": [
                        str(venv_python),
                        "-m",
                        "pip",
                        "install",
                        "--quiet",
                        "--disable-pip-version-check",
                        "-e",
                        ".",
                        "pytest",
                        "tox",
                    ],
                    "env": {},
                },
            )
        )
    return commands


def _environment_fingerprint(root: Path, ensure_project_env: bool) -> str:
    digest = hashlib.sha256()
    digest.update(f"python={sys.version}\nproject={ensure_project_env}\n".encode())
    for rel_path in ("agent_tools/requirements-mcp.txt", "pyproject.toml", "tox.ini"):
        path = root / rel_path
        digest.update(f"{rel_path}:".encode())
        if path.is_file():
            digest.update(path.read_bytes())
        else:
            digest.update(b"<missing>")
    return digest.hexdigest()


def _environment_ready(
    root: Path,
    *,
    ensure_project_env: bool,
    fingerprint: str,
) -> tuple[bool, dict[str, Any] | None]:
    state_path = root / ENV_STATE_FILE
    venv_python = root / ".venv" / "bin" / "python"
    if not state_path.is_file() or not venv_python.is_file():
        return False, None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, None
    if (
        state.get("fingerprint") != fingerprint
        or state.get("ensure_project_env") is not ensure_project_env
    ):
        return False, None
    imports = "import mcp"
    if ensure_project_env:
        imports += "; import analytics_toolkit, pytest, tox"
    result = _run_command(
        root,
        {
            "display": ".venv/bin/python agent environment health check",
            "args": [str(venv_python), "-c", imports],
            "env": {},
        },
    )
    return bool(result["ok"]), result


def _write_environment_state(
    root: Path,
    *,
    fingerprint: str,
    ensure_project_env: bool,
) -> None:
    state_path = root / ENV_STATE_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "ensure_project_env": ensure_project_env,
                "python": sys.version,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _compact_repo_health(health: dict[str, Any]) -> dict[str, Any]:
    compact = {
        key: health[key]
        for key in ("branch", "dirty", "status_short", "diff_stat", "staged_diff_stat")
        if key in health
    }
    compact["status_count"] = len(health.get("status_short", []))
    compact["diff_stat_count"] = len(health.get("diff_stat", []))
    compact["staged_diff_stat_count"] = len(health.get("staged_diff_stat", []))
    return compact


def _compact_metadata_status(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        key: metadata[key]
        for key in ("ok", "package_version", "readme_version", "latest_changelog", "blockers")
        if key in metadata
    }


def _startup_context_id(task: str, module: str | None) -> str:
    value = json.dumps({"task": task, "module": module}, sort_keys=True)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _write_startup_context(
    root: Path,
    *,
    task: str,
    module: str | None,
    required_files: list[str],
    repository_state: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    state = {
        "schema_version": 1,
        "id": _startup_context_id(task, module),
        "task": task,
        "module": module,
        "required_instruction_files": required_files,
        "repo_health": _compact_repo_health(repository_state["repo_health"]),
        "metadata_status": _compact_metadata_status(repository_state["metadata_status"]),
        "updated_at": time.time(),
    }
    path = root / STARTUP_CONTEXT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    metrics_path = root / TOOL_METRICS_FILE
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text("", encoding="utf-8")
    metrics_path.chmod(0o600)
    return state


def _read_startup_context(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / STARTUP_CONTEXT_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_tool_metrics(root: Path, session_id: str) -> list[dict[str, Any]]:
    if not session_id:
        return []
    try:
        lines = (root / TOOL_METRICS_FILE).read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    entries: list[dict[str, Any]] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("session_id") == session_id:
            entries.append(entry)
    return entries


def _record_tool_metric(  # noqa: PLR0913 - persisted metric fields stay explicit.
    root: Path,
    *,
    tool: str,
    ok: bool,
    response_bytes: int,
    raw_output_bytes: int,
    duration_seconds: float,
    failure_signature: str | None,
) -> None:
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    context = _read_startup_context(root)
    session_id = str(context.get("id", ""))
    if not session_id:
        return
    entry = {
        "session_id": session_id,
        "tool": tool,
        "ok": ok,
        "response_bytes": response_bytes,
        "raw_output_bytes": raw_output_bytes,
        "duration_seconds": round(duration_seconds, 3),
        "failure_signature": failure_signature,
        "recorded_at": time.time(),
    }
    path = root / TOOL_METRICS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")
    path.chmod(0o600)


def _workflow_context(
    root: Path,
    *,
    task: str,
    module: str | None,
    repository_state: dict[str, dict[str, Any]],
    update: bool,
) -> dict[str, Any]:
    path = root / STARTUP_CONTEXT_FILE
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        state = {}
    context_id = _startup_context_id(task, module)
    reused = state.get("id") == context_id
    current_health = _compact_repo_health(repository_state["repo_health"])
    current_metadata = _compact_metadata_status(repository_state["metadata_status"])
    changes: dict[str, Any] = {}
    if reused:
        health_changes = {
            key: {"before": state.get("repo_health", {}).get(key), "after": value}
            for key, value in current_health.items()
            if state.get("repo_health", {}).get(key) != value
        }
        metadata_changes = {
            key: {"before": state.get("metadata_status", {}).get(key), "after": value}
            for key, value in current_metadata.items()
            if state.get("metadata_status", {}).get(key) != value
        }
        if health_changes:
            changes["repo_health"] = health_changes
        if metadata_changes:
            changes["metadata_status"] = metadata_changes
    if update:
        _write_startup_context(
            root,
            task=task,
            module=module,
            required_files=list(state.get("required_instruction_files", [])) if reused else [],
            repository_state=repository_state,
        )
    return {"id": context_id, "reused": reused, "changes": changes}


def _check_commands(  # noqa: PLR0913 - mirrors the public check workflow inputs.
    area: str | None,
    change_type: str,
    level: str,
    integration_profile: str = "all",
    integration_clickhouse_driver: str = "both",
    root: Path | None = None,
) -> list[dict[str, Any]]:
    if integration_profile not in INTEGRATION_PROFILES:
        expected = ", ".join(INTEGRATION_PROFILES)
        message = f"integration_profile must be one of: {expected}"
        raise ValueError(message)
    if integration_clickhouse_driver not in INTEGRATION_CLICKHOUSE_DRIVERS:
        expected = ", ".join(INTEGRATION_CLICKHOUSE_DRIVERS)
        message = f"integration_clickhouse_driver must be one of: {expected}"
        raise ValueError(message)
    if level != "integration" and integration_profile != "all":
        message = "integration_profile is only valid for level='integration'"
        raise ValueError(message)
    if level == "focused":
        key = _normalize_area(area or "")
        if key == "sql":
            return [_sql_focused_command(root or REPO_ROOT)]
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
    if level == "integration":
        if _normalize_area(area or "") != "sql":
            message = "level='integration' is only supported for area='sql'"
            raise ValueError(message)
        return [
            {
                "display": (
                    "python -m release_routines.sql_integration "
                    f"--profile {integration_profile} "
                    f"--clickhouse-driver {integration_clickhouse_driver}"
                ),
                "args": [
                    sys.executable,
                    "-m",
                    "release_routines.sql_integration",
                    "--profile",
                    integration_profile,
                    "--clickhouse-driver",
                    integration_clickhouse_driver,
                ],
                "env": {},
            }
        ]
    if level == "precommit":
        return PRECOMMIT_CHECK_COMMANDS
    if level == "release":
        return RELEASE_CHECK_COMMANDS
    msg = "level must be 'focused', 'integration', 'precommit', or 'release'"
    raise ValueError(msg)


def _sql_focused_command(_root: Path) -> dict[str, Any]:
    tests = ["tests/sql"]
    return {
        "display": ("PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q " + " ".join(tests)),
        "args": ["pytest", "-q", *tests],
        "env": {"PYTHONPYCACHEPREFIX": PYTHON_CACHE_DIR},
    }


def _run_command(root: Path, command: dict[str, Any]) -> dict[str, Any]:
    env = os.environ.copy()
    env.update(command.get("env") or {})
    venv_bin = root / ".venv" / "bin"
    if venv_bin.is_dir():
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
    started = time.monotonic()
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
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    result = {
        "ok": completed.returncode == 0,
        "command": _command_display(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "summary": _command_summary(completed.stdout, completed.stderr),
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    if command.get("persist_output", True) and (completed.stdout or completed.stderr):
        result["log_ref"] = _persist_command_output(root, result)
    return result


def _run_git_pull(root: Path) -> dict[str, Any]:
    return _run_git(root, ["pull", "origin", "main"])


def _run_git(root: Path, args: list[str]) -> dict[str, Any]:
    return _run_command(
        root, {"display": f"git {' '.join(args)}", "args": ["git", *args], "env": {}}
    )


def _command_display(command: dict[str, Any]) -> str:
    return str(command["display"])


def _detail_error(detail: str) -> str:
    return f"detail must be one of: {', '.join(DETAIL_LEVELS)}; received {detail!r}"


def _command_summary(stdout: str, stderr: str, max_chars: int = 500) -> str:
    text = (stdout.strip() or stderr.strip()).replace("\n", " ")
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def _persist_command_output(root: Path, result: dict[str, Any]) -> str:
    log_dir = root / TOOL_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    command = str(result.get("command", "command"))
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:10]
    path = log_dir / f"{time.time_ns()}-{digest}.log"
    text = (
        f"command: {command}\n"
        f"returncode: {result.get('returncode')}\n\n"
        f"[stdout]\n{result.get('stdout', '')}\n\n"
        f"[stderr]\n{result.get('stderr', '')}\n"
    )
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path.relative_to(root).as_posix()


def _bounded_text(value: Any, max_chars: int = DIAGNOSTIC_EXCERPT_CHARS) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return f"{text[:half]}\n... output omitted ...\n{text[-half:]}"


def _compact_command_result(result: dict[str, Any], detail: str) -> dict[str, Any]:
    compact = {
        "ok": bool(result.get("ok")),
        "command": result.get("command"),
        "returncode": result.get("returncode"),
        "duration_seconds": result.get("duration_seconds"),
        "summary": _bounded_text(result.get("summary"), 500),
    }
    if result.get("log_ref"):
        compact["log_ref"] = result["log_ref"]
    if detail == "full":
        compact["stdout"] = str(result.get("stdout", ""))
        compact["stderr"] = str(result.get("stderr", ""))
    return compact


def _raw_command_output_bytes(results: list[dict[str, Any]]) -> int:
    return sum(
        len(str(result.get("stdout", "")).encode("utf-8"))
        + len(str(result.get("stderr", "")).encode("utf-8"))
        for result in results
    )


def _command_blocker(phase: str, result: dict[str, Any]) -> dict[str, Any]:
    blocker = {
        "phase": phase,
        "command": result.get("command"),
        "returncode": result.get("returncode"),
    }
    stderr = str(result.get("stderr") or "").strip()
    stdout = str(result.get("stdout") or "").strip()
    stderr_lines = [line for line in stderr.splitlines() if line.strip()]
    stderr_is_stage_only = bool(stderr_lines) and all(
        line.startswith("::agent-check-stage::") for line in stderr_lines
    )
    excerpt = _bounded_text(stdout if stderr_is_stage_only else stderr or stdout)
    if excerpt:
        blocker["excerpt"] = excerpt
    if result.get("log_ref"):
        blocker["log_ref"] = result["log_ref"]
    return blocker


def _check_failure_blocker(result: dict[str, Any]) -> dict[str, Any]:
    output = "\n".join(
        part for part in (str(result.get("stdout", "")), str(result.get("stderr", ""))) if part
    )
    phase = "run_checks"
    marker_matches = re.findall(r"::agent-check-stage::([^:]+)::(?:start|end)::([^\s]+)", output)
    stage = marker_matches[-1][0] if marker_matches else None
    failed_stages = _unique(re.findall(r"::agent-check-stage::([^:]+)::end::failed", output))
    if "Coverage targets raised; review and rerun:" in output:
        phase = "coverage_ratchet_confirmation"
        stage = stage or "coverage"
    blocker = _command_blocker(phase, result)
    if stage:
        blocker["stage"] = stage
    if failed_stages:
        blocker["failed_stages"] = failed_stages
    node_ids = sorted(set(re.findall(r"(?:FAILED|ERROR)\s+(tests/[^\s]+::[^\s]+)", output)))
    if node_ids:
        blocker["test_node_ids"] = node_ids[:25]
    architecture = sorted(
        set(re.findall(r"analytics_toolkit/sql/[^\s]+\.py has \d+ lines", output))
    )
    if architecture:
        blocker["architecture_violations"] = architecture
    debt_lines = [
        line.strip()
        for line in output.splitlines()
        if re.search(r"\[[A-Z][A-Z0-9-]+\]: \d+ current, \d+ baseline", line)
    ]
    if debt_lines:
        blocker["quality_debt_increases"] = debt_lines[:50]
    tox_failures = sorted(
        set(
            re.findall(
                r"^\s*([a-z0-9-]+):\s+(?:FAIL|failed)",
                output,
                flags=re.MULTILINE | re.IGNORECASE,
            )
        )
    )
    if tox_failures:
        blocker["tox_environments"] = tox_failures
    if phase == "coverage_ratchet_confirmation":
        marker = "Coverage targets raised; review and rerun:"
        changes = output.split(marker, 1)[1].strip().splitlines()
        blocker["target_changes"] = [line.strip() for line in changes if line.strip()][:50]
    return blocker


def _annotate_check_failure(
    root: Path,
    blocker: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    stable = {key: value for key, value in blocker.items() if key not in {"excerpt", "log_ref"}}
    signature = hashlib.sha256(
        json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    state_path = root / CHECK_FAILURE_STATE_FILE
    try:
        previous = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        previous = {}
    changed = previous.get("failure_signature") != signature
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {"failure_signature": signature, "updated_at": time.time()},
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    state_path.chmod(0o600)
    compact = dict(blocker)
    if not changed:
        compact.pop("excerpt", None)
        compact["unchanged"] = True
    return compact, {"failure_signature": signature, "failure_changed": changed}


def _managed_coverage_changes(results: list[dict[str, Any]]) -> list[str]:
    marker = "Coverage targets raised; managed update accepted:"
    for result in results:
        output = "\n".join(str(result.get(key, "")) for key in ("stdout", "stderr"))
        if marker not in output:
            continue
        tail = output.split(marker, 1)[1]
        return [
            line.strip()
            for line in tail.splitlines()
            if line.strip() and not line.startswith("::agent-check-stage::")
        ][:50]
    return []


def _check_next_action(blocker: dict[str, Any], level: str) -> str:
    if blocker.get("phase") == "coverage_ratchet_confirmation":
        return (
            f"Review the monotonic coverage target changes, then rerun run_checks(level={level!r})."
        )
    stage = blocker.get("stage")
    if stage:
        return f"Fix the {stage} stage failure, then rerun run_checks(level={level!r})."
    return f"Fix the failure, then rerun run_checks(level={level!r})."


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
    detail = str(input_summary.get("detail", "summary"))
    if detail not in DETAIL_LEVELS:
        detail = "summary"
    raw_results = command_results or []
    compact_results = (
        []
        if detail == "summary"
        else [_compact_command_result(item, detail) for item in raw_results]
    )
    raw_bytes = _raw_command_output_bytes(raw_results)
    returned_bytes = (
        len(json.dumps(compact_results, ensure_ascii=False, default=str).encode("utf-8"))
        if compact_results
        else 0
    )
    normalized_blockers = []
    for blocker in blockers or []:
        normalized = dict(blocker)
        excerpts = [normalized.pop(key, "") for key in ("excerpt", "stderr", "stdout")]
        excerpt = next((str(value) for value in excerpts if str(value).strip()), "")
        if excerpt:
            normalized["excerpt"] = _bounded_text(excerpt)
        normalized_blockers.append(normalized)
    payload = {
        "ok": ok,
        "tool": tool,
        "input": _compact_input_summary(input_summary, detail=detail),
        "summary": summary,
        "result": result or {},
        "command_results": compact_results,
        "blockers": normalized_blockers,
        "next_actions": next_actions or [],
    }
    budget = _response_budget(tool, detail=detail, ok=ok, result=payload["result"])
    telemetry: dict[str, Any] = {
        "response_budget_bytes": budget,
        "within_budget": True,
        "response_bytes": 0,
        "truncated": False,
    }
    if detail != "summary":
        telemetry.update(
            {
                "raw_output_bytes": raw_bytes,
                "returned_output_bytes": returned_bytes,
                "suppressed_output_bytes": max(0, raw_bytes - returned_bytes),
                "section_bytes": {
                    key: len(
                        json.dumps(payload[key], ensure_ascii=False, default=str).encode("utf-8")
                    )
                    for key in ("input", "result", "command_results", "blockers", "next_actions")
                },
            }
        )
    payload["telemetry"] = telemetry
    if budget is not None and _serialized_bytes(payload) > budget:
        _compact_payload_to_budget(payload, budget)
    for _ in range(3):
        payload["telemetry"]["response_bytes"] = _serialized_bytes(payload)
    payload["telemetry"]["within_budget"] = (
        budget is None or payload["telemetry"]["response_bytes"] <= budget
    )
    root_value = input_summary.get("root", ".")
    try:
        metrics_root = _resolve_root(str(root_value))
        signature = str(payload["result"].get("failure_signature", "")) or None
        _record_tool_metric(
            metrics_root,
            tool=tool,
            ok=ok,
            response_bytes=int(payload["telemetry"]["response_bytes"]),
            raw_output_bytes=raw_bytes,
            duration_seconds=sum(float(item.get("duration_seconds", 0)) for item in raw_results),
            failure_signature=signature,
        )
    except OSError:
        pass
    return payload


def _serialized_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _bounded_structure(value: Any, *, max_items: int, max_string: int) -> Any:
    if isinstance(value, str):
        return _bounded_text(value, max_string)
    if isinstance(value, list):
        return [
            _bounded_structure(item, max_items=max_items, max_string=max_string)
            for item in value[:max_items]
        ]
    if isinstance(value, dict):
        return {
            key: _bounded_structure(item, max_items=max_items, max_string=max_string)
            for key, item in value.items()
        }
    return value


def _compact_payload_to_budget(payload: dict[str, Any], budget: int) -> None:
    payload["telemetry"]["truncated"] = True
    for max_items, max_string in ((8, 400), (5, 240), (3, 120)):
        payload["result"] = _bounded_structure(
            payload["result"], max_items=max_items, max_string=max_string
        )
        payload["blockers"] = _bounded_structure(
            payload["blockers"], max_items=max_items, max_string=max_string
        )
        payload["next_actions"] = _bounded_structure(
            payload["next_actions"], max_items=max_items, max_string=max_string
        )
        payload["command_results"] = _bounded_structure(
            payload["command_results"], max_items=max_items, max_string=max_string
        )
        if _serialized_bytes(payload) <= budget:
            return
    payload["result"] = {
        "truncated": True,
        "available_via": "Use diagnostic detail or the returned log_ref for bounded evidence.",
    }
    payload["blockers"] = _bounded_structure(payload["blockers"], max_items=2, max_string=100)
    payload["next_actions"] = _bounded_structure(
        payload["next_actions"], max_items=1, max_string=100
    )
    payload["command_results"] = []
    if _serialized_bytes(payload) > budget:
        payload["blockers"] = [
            {key: item[key] for key in ("phase", "message", "log_ref") if key in item}
            for item in payload["blockers"][:1]
            if isinstance(item, dict)
        ]
        payload["telemetry"] = {
            "response_budget_bytes": budget,
            "within_budget": True,
            "response_bytes": 0,
            "truncated": True,
        }


def _compact_input_summary(input_summary: dict[str, Any], *, detail: str) -> dict[str, Any]:
    if detail != "summary":
        return input_summary
    compact: dict[str, Any] = {}
    defaults = {
        "change_type": "implementation",
        "check_timeout_seconds": GITHUB_CHECK_TIMEOUT_SECONDS,
        "detail": "summary",
        "dry_run": False,
        "ensure_project_env": True,
        "force_release": False,
        "index_dir": DEFAULT_INDEX_DIR,
        "integration_profile": "all",
        "integration_clickhouse_driver": "both",
        "mode": "search",
        "top_k": 3,
        "wait_seconds": GITHUB_CHECK_WAIT_SECONDS,
    }
    for key, value in input_summary.items():
        if value is None or defaults.get(key) == value or key == "resolved_index_dir":
            continue
        if key == "root" and Path(str(value)).resolve() == REPO_ROOT.resolve():
            continue
        if key == "paths" and isinstance(value, list):
            compact["path_count"] = len(value)
            continue
        compact[key] = value
    return compact


def _response_budget(
    tool: str,
    *,
    detail: str,
    ok: bool,
    result: dict[str, Any],
) -> int | None:
    if detail == "full":
        return None
    if detail == "diagnostic":
        return 6000 if not ok else 5000
    if not ok:
        return 4000
    github = result.get("github_checks", {}) if isinstance(result, dict) else {}
    if tool == "git_workflow" and github.get("status") == "pending":
        return 1500
    return {
        "change_impact": 6000,
        "docs": 3500,
        "git_workflow": 3000,
        "prepare_start": 2500,
        "release_workflow": 5000,
        "run_checks": 2500,
        "workflow_metrics": 3000,
        "workflow_status": 2500,
    }.get(tool, 3000)


def _search_result_to_dict(
    result: docs_assistant.SearchResult,
    *,
    detail: str = "summary",
) -> dict[str, Any]:
    chunk = result.chunk
    compact = {
        "citation": chunk.citation,
        "heading": chunk.heading,
        "snippet": docs_assistant.snippet(chunk.text, 180),
    }
    if detail != "summary":
        compact.update(
            {
                "score": result.score,
                "lexical_score": result.lexical_score,
                "path": chunk.path,
                "source_type": chunk.source_type,
            }
        )
    return compact


def _dedupe_search_results(
    results: list[docs_assistant.SearchResult],
) -> list[docs_assistant.SearchResult]:
    unique_results: list[docs_assistant.SearchResult] = []
    seen: set[tuple[str, str]] = set()
    for item in results:
        key = (item.chunk.citation, item.chunk.heading)
        if key in seen:
            continue
        seen.add(key)
        unique_results.append(item)
    return unique_results


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
        msg = f"Could not parse {label}"
        raise ValueError(msg)
    return match.group(1)


def _replace_required(pattern: re.Pattern[str], text: str, replacement: str, label: str) -> str:
    updated, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        msg = f"Could not update {label}"
        raise ValueError(msg)
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
        msg = "Could not update changelog"
        raise ValueError(msg)
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
    bullets: list[list[str]] = []
    for line in text[body_start:section_end].splitlines():
        if line.startswith("- "):
            bullets.append([line.rstrip()])
        elif bullets and (line.startswith("  ") or line.startswith("\t")):
            bullets[-1].append(line.rstrip())
    return ["\n".join(lines) for lines in bullets]


def _count_unreleased_changelog_bullets(text: str) -> int:
    return len(_unreleased_changelog_bullets(text))


def _upsert_unreleased_changelog_bullet(text: str, bullet: str) -> str:
    if not text.strip():
        msg = "Could not update changelog"
        raise ValueError(msg)
    bounds = _unreleased_changelog_bounds(text)
    if bounds is None:
        entry = f"## Unreleased\n\n{bullet}\n\n"
        match = re.search(r"^##\s+", text, flags=re.MULTILINE)
        if match is None:
            return text.rstrip() + "\n\n" + entry.rstrip() + "\n"
        return text[: match.start()] + entry + text[match.start() :]

    _section_start, body_start, section_end = bounds
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
        msg = "Version must have four numeric parts"
        raise ValueError(msg)
    numbers = [int(part) for part in parts]
    if any(number < 0 or number > 19 for number in numbers):
        msg = "Version components must be between 0 and 19"
        raise ValueError(msg)

    for index in range(3, -1, -1):
        if numbers[index] < 19:
            numbers[index] += 1
            for reset_index in range(index + 1, 4):
                numbers[reset_index] = 0
            return ".".join(str(number) for number in numbers)
    msg = "Version cannot be incremented without exceeding component limits"
    raise ValueError(msg)


def _project_dependencies(pyproject_text: str) -> list[str]:
    match = re.search(
        r"^dependencies\s*=\s*\[(.*?)^\]", pyproject_text, flags=re.MULTILINE | re.DOTALL
    )
    if match is None:
        return []
    return DEPENDENCY_RE.findall(match.group(1))


def _optional_dependencies(pyproject_text: str) -> dict[str, list[str]]:
    optional: dict[str, list[str]] = {}
    match = re.search(
        r"^\[project\.optional-dependencies\]\s*(.*?)(?:^\[|\Z)",
        pyproject_text,
        flags=re.MULTILINE | re.DOTALL,
    )
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
        notes.append(
            "Implementation, testing, build, or commit work requires development instructions."
        )
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
            missing.append(
                "Run version_bump(...) so pyproject.toml, README.md, and docs/CHANGELOG.md align."
            )
        if not _verify_precommit_success(root)["ok"]:
            missing.append(
                "Run run_checks(level='precommit') before git_workflow(action='commit')."
            )
        visual = sql_explorer_visual.verify_visual_receipt(root)
        if not visual["ok"]:
            missing.append(
                "Complete the full fresh-macOS-VM SQL Explorer visual review before commit."
            )
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
    return {path for path in result["stdout"].splitlines() if path} or {rel_path}


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
        normalized in {"README.md", "AGENTS.md", "agent_tools/README.md"}
        or normalized.startswith(("docs/", "agent_docs/"))
        or (normalized.endswith(".md") and "/" not in normalized)
    )


def _is_sensitive_local_path(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/").strip("/")
    if not normalized:
        return False
    parts = normalized.split("/")
    return normalized in SENSITIVE_LOCAL_PATHS or any(
        _is_sensitive_path_part(part) for part in parts
    )


def _is_sensitive_path_part(part: str) -> bool:
    return (
        part in SENSITIVE_LOCAL_PATHS
        or part in SENSITIVE_LOCAL_DIRS
        or part.startswith((".env.", ".env-"))
    )


def _tracked_diff_fingerprint_parts(root: Path, *, staged: bool) -> list[str]:
    label = "staged-diff" if staged else "working-diff"
    if staged:
        changed = _require_git_for_fingerprint(root, ["diff", "--cached", "--raw", "-z"])
        return [label, changed.get("stdout", ""), changed.get("stderr", "")]
    changed = _require_git_for_fingerprint(root, ["diff", "--name-only", "-z"])
    parts = [label, changed.get("stderr", "")]
    for rel_path in sorted(path for path in changed.get("stdout", "").split("\0") if path):
        if _is_sensitive_local_path(rel_path):
            parts.append(f"{rel_path}:excluded-sensitive-local-path")
            continue
        parts.append(f"{rel_path}:{_working_path_digest(root / rel_path)}")
    return parts


def _working_path_digest(path: Path) -> str:
    try:
        if path.is_symlink():
            return "symlink:" + hashlib.sha256(os.readlink(path).encode("utf-8")).hexdigest()
        if not path.exists():
            return "deleted"
        if not path.is_file():
            return "non-file"
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}:{exc}"


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


def _precommit_command_fingerprint(command: dict[str, Any]) -> str:
    payload = {
        "display": command.get("display"),
        "args": command.get("args", []),
        "env": command.get("env", {}),
        "stage": command.get("stage"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _precommit_toolchain_fingerprint(root: Path) -> str:
    evidence = [
        f"python={sys.version}",
        f"executable={sys.executable}",
        f"parallelism={os.environ.get('PRECOMMIT_PARALLELISM', '3')}",
    ]
    evidence.extend(
        _precommit_tool_evidence(root, command)
        for command in (
            ["tox", "--version"],
            ["pyenv", "--version"],
            ["pyenv", "versions", "--bare"],
        )
    )
    return hashlib.sha256("\n".join(evidence).encode("utf-8")).hexdigest()


def _precommit_tool_evidence(root: Path, command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return f"{' '.join(command)}=unavailable:{type(exc).__name__}"
    output = (completed.stdout + completed.stderr).strip()
    return f"{' '.join(command)}={completed.returncode}:{output}"


def _load_precommit_stage_state(root: Path) -> dict[str, Any]:
    state_path = root / PRECOMMIT_STAGE_STATE_FILE
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"version": 1, "stages": {}}
    if not isinstance(state, dict) or not isinstance(state.get("stages"), dict):
        return {"version": 1, "stages": {}}
    return state


def _precommit_stage_receipt_is_current(
    receipt: Any,
    *,
    fingerprint: str,
    toolchain_fingerprint: str,
    command: dict[str, Any],
) -> bool:
    if not isinstance(receipt, dict):
        return False
    completed_at = receipt.get("completed_at")
    if not isinstance(completed_at, (int, float)):
        return False
    age_seconds = time.time() - completed_at
    if age_seconds < 0 or age_seconds > PRECOMMIT_STAGE_TTL_SECONDS:
        return False
    return (
        receipt.get("fingerprint") == fingerprint
        and receipt.get("toolchain_fingerprint") == toolchain_fingerprint
        and receipt.get("command_fingerprint") == _precommit_command_fingerprint(command)
    )


def _record_precommit_stage_success(
    root: Path,
    stage: str,
    receipt: dict[str, Any],
) -> None:
    state_path = root / PRECOMMIT_STAGE_STATE_FILE
    state = _load_precommit_stage_state(root)
    state["version"] = 1
    state.setdefault("stages", {})[stage] = receipt
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    state_path.chmod(0o600)


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
    state_path.chmod(0o600)


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
    return {
        "ok": True,
        "message": "Pre-commit check record matches the current tree.",
        "fingerprint": current,
    }


def _push_readiness(root: Path) -> dict[str, Any]:
    health = repo_health(root=str(root))
    blockers: list[dict[str, Any]] = []
    if health["branch"] != WORK_BRANCH:
        blockers.append({"phase": "push", "message": f"Push workflow must run from {WORK_BRANCH}."})
    if health["dirty"]:
        blockers.append(
            {"phase": "push", "message": "Push workflow requires a clean working tree."}
        )

    visual = sql_explorer_visual.verify_visual_receipt(root, for_push=True)
    if not visual["ok"]:
        blockers.append({"phase": "visual_review", "message": visual["message"]})

    remote = _remote_dev_status(root, require_equal=False)
    blockers.extend(remote["blockers"])
    return {
        "repo_health": health,
        "remote_dev_status": remote["result"],
        "sql_explorer_visual_review": visual,
        "command_results": remote["command_results"],
        "blockers": blockers,
    }


def _push_dev_result(root: Path) -> dict[str, Any]:
    readiness = _push_readiness(root)
    public_readiness = {key: value for key, value in readiness.items() if key != "command_results"}
    if readiness["blockers"]:
        return {
            "readiness": public_readiness,
            "command_results": readiness["command_results"],
            "blockers": readiness["blockers"],
        }

    head = _run_git(root, ["rev-parse", "HEAD"])
    if not head["ok"]:
        return {
            "readiness": public_readiness,
            "command_results": [*readiness["command_results"], head],
            "blockers": [_command_blocker("push_sha", head)],
        }
    pushed_sha = head["stdout"].strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", pushed_sha):
        return {
            "readiness": public_readiness,
            "command_results": [*readiness["command_results"], head],
            "blockers": [
                {
                    "phase": "push_sha",
                    "message": "HEAD did not resolve to a full immutable commit SHA.",
                    "sha": pushed_sha,
                }
            ],
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
        "readiness": public_readiness,
        "sha": pushed_sha,
        "push_target": f"origin/{WORK_BRANCH}",
        "command_results": [*readiness["command_results"], head, result],
        "blockers": blockers,
    }


def _push_dev_workflow(
    root: Path,
    input_summary: dict[str, Any],
    *,
    timeout_seconds: int,
    wait_seconds: int,
) -> dict[str, Any]:
    push = _push_dev_result(root)
    if push["blockers"]:
        return _tool_output(
            "git_workflow",
            input_summary,
            ok=False,
            summary="Push failed.",
            result={"push_readiness": push["readiness"]},
            command_results=push["command_results"],
            blockers=push["blockers"],
            next_actions=[
                "Resolve push readiness blockers, then retry git_workflow(action='push')."
            ],
        )

    checks = _watch_pushed_commit(
        root,
        sha=push["sha"],
        timeout_seconds=timeout_seconds,
        wait_seconds=wait_seconds,
        detail=str(input_summary.get("detail", "summary")),
    )
    pending = checks["result"].get("status") == "pending"
    return _tool_output(
        "git_workflow",
        input_summary,
        ok=not checks["blockers"],
        summary=(
            "Push completed; exact-SHA GitHub verification is pending."
            if pending
            else "Push and exact-SHA GitHub verification completed."
            if not checks["blockers"]
            else "Push completed, but exact-SHA GitHub verification failed."
        ),
        result={
            "mutation": {
                "sha": push["sha"],
                "push_target": push["push_target"],
            },
            "github_checks": checks["result"],
        },
        command_results=[*push["command_results"], *checks["command_results"]],
        blockers=checks["blockers"],
        next_actions=(
            [f"Resume with git_workflow(action='checks', sha='{push['sha']}')."]
            if pending or checks["blockers"]
            else []
        ),
    )


def _github_checks_workflow(
    root: Path,
    input_summary: dict[str, Any],
    *,
    sha: str,
    timeout_seconds: int,
    wait_seconds: int,
) -> dict[str, Any]:
    checks = _watch_github_checks(
        root,
        sha=sha,
        timeout_seconds=timeout_seconds,
        wait_seconds=wait_seconds,
        detail=str(input_summary.get("detail", "summary")),
    )
    pending = checks["result"].get("status") == "pending"
    return _tool_output(
        "git_workflow",
        input_summary,
        ok=not checks["blockers"],
        summary=(
            "Exact-SHA GitHub verification is pending."
            if pending
            else "Exact-SHA GitHub verification completed."
            if not checks["blockers"]
            else "Exact-SHA GitHub verification failed."
        ),
        result={"github_checks": checks["result"]},
        command_results=checks["command_results"],
        blockers=checks["blockers"],
        next_actions=(
            [f"Resume with git_workflow(action='checks', sha='{sha}')."] if pending else []
        ),
    )


def _watch_pushed_commit(
    root: Path,
    *,
    sha: str,
    timeout_seconds: int,
    wait_seconds: int,
    detail: str = "summary",
) -> dict[str, Any]:
    """Watch the immutable SHA captured immediately before the push."""
    return _watch_github_checks(
        root,
        sha=sha,
        timeout_seconds=timeout_seconds,
        wait_seconds=wait_seconds,
        detail=detail,
    )


def _watch_github_checks(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0915 - bounded polling state machine.
    root: Path,
    *,
    sha: str,
    timeout_seconds: int = GITHUB_CHECK_TIMEOUT_SECONDS,
    poll_seconds: int = GITHUB_CHECK_POLL_SECONDS,
    discovery_seconds: int = GITHUB_CHECK_DISCOVERY_SECONDS,
    wait_seconds: int | None = None,
    command_runner: Any = None,
    monotonic: Any = None,
    sleeper: Any = None,
    detail: str = "summary",
) -> dict[str, Any]:
    command_runner = command_runner or _run_command
    monotonic = monotonic or time.monotonic
    sleeper = sleeper or time.sleep
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        return _github_check_failure(sha, "validate", "sha must be a Git commit SHA")
    if timeout_seconds <= 0:
        return _github_check_failure(sha, "validate", "timeout must be positive")
    if poll_seconds <= 0 or discovery_seconds <= 0:
        return _github_check_failure(
            sha, "validate", "poll and discovery intervals must be positive"
        )
    if wait_seconds is not None and wait_seconds <= 0:
        return _github_check_failure(sha, "validate", "wait_seconds must be positive")

    try:
        manifest = json.loads((root / REQUIRED_WORKFLOWS_PATH).read_text(encoding="utf-8"))
        if manifest.get("schema_version") != 2:  # noqa: PLR2004 - versioned file contract.
            msg = "required-workflows manifest schema_version must be 2"
            raise ValueError(msg)  # noqa: TRY301 - normalized by the surrounding parser guard.
        branch_manifest = manifest["branches"][WORK_BRANCH]
        workflows = branch_manifest["workflows"]
        conditional_checks = branch_manifest.get("conditional_checks", [])
        if not isinstance(workflows, list) or not workflows:
            msg = "push workflows must be a non-empty list"
            raise ValueError(msg)  # noqa: TRY301 - normalized by the surrounding parser guard.
        allowed_classifications = {"required_push", "advisory_push"}
        if not all(entry.get("classification") in allowed_classifications for entry in workflows):
            msg = "every push workflow must be classified as required_push or advisory_push"
            raise ValueError(msg)  # noqa: TRY301 - normalized by the surrounding parser guard.
        expected = [entry for entry in workflows if entry["classification"] == "required_push"]
        advisory = [entry for entry in workflows if entry["classification"] == "advisory_push"]
        if not expected:
            msg = "at least one required_push workflow must be configured"
            raise ValueError(msg)  # noqa: TRY301 - normalized by the surrounding parser guard.
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return _github_check_failure(sha, "manifest", str(exc))

    started = monotonic()
    elapsed_before = 0.0
    state: dict[str, Any] = {}
    if wait_seconds is not None:
        state = _github_watch_state(
            root,
            sha=sha,
            timeout_seconds=timeout_seconds,
            discovery_seconds=discovery_seconds,
        )
        elapsed_before = max(0.0, time.time() - float(state["started_at"]))
    repository = str(state.get("repository", "")).strip()
    command_results: list[dict[str, Any]] = []
    if not repository:
        repo_result = command_runner(
            root,
            {
                "display": "gh repo view --json nameWithOwner --jq .nameWithOwner",
                "args": [
                    "gh",
                    "repo",
                    "view",
                    "--json",
                    "nameWithOwner",
                    "--jq",
                    ".nameWithOwner",
                ],
                "env": {},
            },
        )
        command_results.append(repo_result)
        if not repo_result["ok"]:
            return {
                "result": {"sha": sha},
                "command_results": command_results,
                "blockers": [_command_blocker("github_auth", repo_result)],
            }
        repository = repo_result["stdout"].strip()
        if wait_seconds is not None:
            _update_github_watch_state(root, sha=sha, repository=repository)
    remaining_timeout = max(0.0, timeout_seconds - elapsed_before)
    remaining_discovery = max(0.0, discovery_seconds - elapsed_before)
    discovery_deadline = started + min(remaining_discovery, remaining_timeout)
    deadline = started + remaining_timeout
    call_deadline = deadline if wait_seconds is None else min(deadline, started + wait_seconds)
    last_snapshot: dict[str, Any] = {"sha": sha, "repository": repository}

    while True:
        snapshot = _github_check_snapshot(
            root,
            repository,
            sha,
            command_runner=command_runner,
        )
        command_results.extend(snapshot.pop("command_results"))
        if snapshot.get("error"):
            return {
                "result": {**last_snapshot, **snapshot},
                "command_results": command_results,
                "blockers": [snapshot["error"]],
            }
        last_snapshot = {
            "sha": sha,
            "repository": repository,
            "conditional_checks": conditional_checks,
            **snapshot,
        }
        classified = _classify_github_snapshot(expected, last_snapshot, advisory=advisory)
        now = monotonic()
        compact = _compact_github_result(
            last_snapshot,
            classified,
            discovery_duration=min(now, discovery_deadline) - started,
            total_duration=now - started,
        )
        if classified["failed"] and not classified["pending"]:
            failed_logs = _failed_run_logs(
                root,
                classified["failed"],
                command_runner=command_runner,
            )
            command_results.extend(failed_logs)
            compact["failed_log_excerpts"] = [
                {
                    "command": item.get("command"),
                    "ok": item.get("ok"),
                    "excerpt": _bounded_text(item.get("stdout") or item.get("stderr") or ""),
                }
                for item in failed_logs
            ]
            compact["status"] = "failed"
            _attach_github_status_changes(root, compact, enabled=wait_seconds is not None)
            return {
                "result": _github_result_receipt(compact, detail=detail),
                "command_results": command_results,
                "blockers": [
                    {
                        "phase": "github_checks",
                        "message": "Required GitHub checks did not succeed.",
                        "failures": classified["failed"],
                    }
                ],
            }
        if not classified["missing"] and not classified["pending"]:
            compact["status"] = "complete"
            _attach_github_status_changes(root, compact, enabled=wait_seconds is not None)
            return {
                "result": _github_result_receipt(compact, detail=detail),
                "command_results": command_results,
                "blockers": [],
            }
        if classified["missing"] and now >= discovery_deadline:
            compact["status"] = "missing"
            _attach_github_status_changes(root, compact, enabled=wait_seconds is not None)
            return {
                "result": _github_result_receipt(compact, detail=detail),
                "command_results": command_results,
                "blockers": [
                    {
                        "phase": "github_checks_discovery",
                        "message": "Required workflows did not appear for the exact SHA.",
                        "missing": classified["missing"],
                    }
                ],
            }
        if now >= deadline:
            compact["status"] = "timed_out"
            _attach_github_status_changes(root, compact, enabled=wait_seconds is not None)
            return {
                "result": _github_result_receipt(compact, detail=detail),
                "command_results": command_results,
                "blockers": [
                    {
                        "phase": "github_checks_timeout",
                        "message": "Timed out waiting for exact-SHA GitHub checks.",
                        "missing": classified["missing"],
                        "pending": classified["pending"],
                    }
                ],
            }
        if now >= call_deadline:
            compact.update(
                {
                    "status": "pending",
                    "watch_id": sha,
                    "resume_after_seconds": poll_seconds,
                }
            )
            _attach_github_status_changes(root, compact, enabled=True)
            return {
                "result": _github_result_receipt(compact, detail=detail),
                "command_results": command_results,
                "blockers": [],
            }
        sleeper(min(poll_seconds, max(0.0, call_deadline - now)))


def _github_check_snapshot(
    root: Path,
    repository: str,
    sha: str,
    *,
    command_runner: Any = None,
) -> dict[str, Any]:
    command_runner = command_runner or _run_command
    endpoints = {
        "runs": f"repos/{repository}/actions/runs?head_sha={sha}&event=push&per_page=100",
        "check_runs": f"repos/{repository}/commits/{sha}/check-runs?per_page=100",
        "statuses": f"repos/{repository}/commits/{sha}/status",
    }
    payloads: dict[str, Any] = {}
    results: list[dict[str, Any]] = []
    for key, endpoint in endpoints.items():
        result = command_runner(
            root,
            {
                "display": f"gh api {endpoint}",
                "args": ["gh", "api", endpoint],
                "env": {},
                "persist_output": False,
            },
        )
        results.append(result)
        if not result["ok"]:
            return {
                "command_results": results,
                "error": _command_blocker("github_api", result),
            }
        try:
            payloads[key] = json.loads(result["stdout"] or "{}")
        except json.JSONDecodeError as exc:
            return {
                "command_results": results,
                "error": {"phase": "github_api", "message": str(exc)},
            }

    runs = payloads["runs"].get("workflow_runs", [])
    jobs: list[dict[str, Any]] = []
    for run in runs:
        endpoint = f"repos/{repository}/actions/runs/{run['id']}/jobs?per_page=100"
        result = command_runner(
            root,
            {
                "display": f"gh api {endpoint}",
                "args": ["gh", "api", endpoint],
                "env": {},
                "persist_output": False,
            },
        )
        results.append(result)
        if not result["ok"]:
            return {
                "command_results": results,
                "error": _command_blocker("github_jobs", result),
            }
        try:
            run_jobs = json.loads(result["stdout"] or "{}").get("jobs", [])
        except json.JSONDecodeError as exc:
            return {
                "command_results": results,
                "error": {"phase": "github_jobs", "message": str(exc)},
            }
        jobs.extend({**job, "workflow_run_id": run["id"]} for job in run_jobs)
    return {
        "runs": runs,
        "jobs": jobs,
        "check_runs": payloads["check_runs"].get("check_runs", []),
        "statuses": payloads["statuses"].get("statuses", []),
        "command_results": [],
    }


def _github_watch_state(
    root: Path,
    *,
    sha: str,
    timeout_seconds: int,
    discovery_seconds: int,
) -> dict[str, Any]:
    state_dir = root / GITHUB_WATCH_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / f"{sha}.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        state = {}
    if (
        state.get("sha") != sha
        or state.get("timeout_seconds") != timeout_seconds
        or state.get("discovery_seconds") != discovery_seconds
    ):
        state = {
            "sha": sha,
            "started_at": time.time(),
            "timeout_seconds": timeout_seconds,
            "discovery_seconds": discovery_seconds,
        }
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        state_path.chmod(0o600)
    return state


def _update_github_watch_state(root: Path, *, sha: str, **values: Any) -> None:
    state_path = root / GITHUB_WATCH_DIR / f"{sha}.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        state = {"sha": sha, "started_at": time.time()}
    state.update(values)
    state["updated_at"] = time.time()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    state_path.chmod(0o600)


def _attach_github_status_changes(
    root: Path,
    compact: dict[str, Any],
    *,
    enabled: bool,
) -> None:
    if not enabled:
        return
    sha = str(compact["sha"])
    state_path = root / GITHUB_WATCH_DIR / f"{sha}.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        state = {"sha": sha, "started_at": time.time()}
    items: list[dict[str, Any]] = []
    for workflow in compact.get("required", []):
        items.append(
            {
                "name": workflow["name"],
                "kind": "workflow",
                "status": workflow.get("status"),
                "conclusion": workflow.get("conclusion"),
                "url": workflow.get("url"),
            }
        )
        items.extend(
            {
                "name": f"{workflow['name']}: {job['name']}",
                "kind": "job",
                "status": job.get("status"),
                "conclusion": job.get("conclusion"),
                "url": job.get("url"),
            }
            for job in workflow.get("jobs", [])
        )
    for workflow in compact.get("advisory", []):
        items.append(
            {
                "name": workflow["name"],
                "kind": "advisory_workflow",
                "status": workflow.get("status"),
                "conclusion": workflow.get("conclusion"),
                "url": workflow.get("url"),
            }
        )
        items.extend(
            {
                "name": f"{workflow['name']}: {job['name']}",
                "kind": "advisory_job",
                "status": job.get("status"),
                "conclusion": job.get("conclusion"),
                "url": job.get("url"),
            }
            for job in workflow.get("jobs", [])
        )
    items.extend(
        {
            "name": str(check.get("name")),
            "kind": "check_run",
            "status": check.get("status"),
            "conclusion": check.get("conclusion"),
            "url": check.get("url"),
        }
        for check in compact.get("check_runs", [])
    )
    items.extend(
        {
            "name": str(status.get("name")),
            "kind": "commit_status",
            "status": status.get("state"),
            "conclusion": status.get("state") if status.get("state") != "pending" else None,
            "url": status.get("url"),
        }
        for status in compact.get("status_contexts", [])
    )
    current = {
        f"{item['kind']}:{item['name']}": {
            key: item.get(key)
            for key in ("kind", "status", "conclusion", "url")
            if item.get(key) is not None
        }
        for item in items
    }
    previous = state.get("reported_statuses", {})
    compact["changes"] = [
        {
            "name": key.split(":", 1)[1],
            "kind": value["kind"],
            "before": previous.get(key),
            "after": value,
        }
        for key, value in current.items()
        if previous.get(key) != value
    ]
    state["reported_statuses"] = current
    state["last_status"] = compact.get("status")
    state["updated_at"] = time.time()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    state_path.chmod(0o600)


def _classify_github_snapshot(  # noqa: C901, PLR0912, PLR0915 - checks multiple GitHub state kinds.
    expected: list[dict[str, Any]],
    snapshot: dict[str, Any],
    *,
    advisory: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    advisory = advisory or []
    runs_by_name: dict[str, dict[str, Any]] = {}
    for run in snapshot.get("runs", []):
        name = run.get("name")
        if not name:
            continue
        current = runs_by_name.get(name)
        rank = (int(run.get("run_attempt") or 1), int(run.get("id") or 0))
        current_rank = (
            (
                int(current.get("run_attempt") or 1),
                int(current.get("id") or 0),
            )
            if current
            else (-1, -1)
        )
        if rank > current_rank:
            runs_by_name[name] = run
    jobs = snapshot.get("jobs", [])
    required: list[dict[str, Any]] = []
    advisory_results: list[dict[str, Any]] = []
    missing: list[str] = []
    pending: list[str] = []
    failed: list[dict[str, Any]] = []
    for entry in expected:
        name = entry["name"]
        run = runs_by_name.get(name)
        if run is None:
            missing.append(name)
            continue
        item = {
            "name": name,
            "run_id": run.get("id"),
            "attempt": run.get("run_attempt", 1),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "url": run.get("html_url"),
            "jobs": [],
        }
        required.append(item)
        allowed = set(entry.get("allowed_conclusions", ["success"]))
        if run.get("status") != "completed":
            pending.append(name)
        elif run.get("conclusion") not in allowed:
            failed.append(item)
        required_jobs = entry.get("required_jobs", [])
        run_jobs = [job for job in jobs if job.get("workflow_run_id") == run.get("id")]
        for job_entry in required_jobs:
            if isinstance(job_entry, str):
                job_name = job_entry
                job_allowed = allowed
            else:
                job_name = job_entry["name"]
                job_allowed = set(job_entry.get("allowed_conclusions", ["success"]))
            job = next(
                (candidate for candidate in run_jobs if candidate.get("name") == job_name), None
            )
            if job is None:
                missing.append(f"{name}: {job_name}")
                continue
            job_item = {
                "name": job_name,
                "status": job.get("status"),
                "conclusion": job.get("conclusion"),
                "url": job.get("html_url"),
                "failed_steps": [
                    {
                        "name": step.get("name"),
                        "conclusion": step.get("conclusion"),
                    }
                    for step in job.get("steps", [])
                    if step.get("conclusion") not in {None, "success", "skipped"}
                ],
            }
            item["jobs"].append(job_item)
            if job.get("status") != "completed":
                pending.append(f"{name}: {job_name}")
            elif job.get("conclusion") not in job_allowed:
                failed.append({**job_item, "workflow": name, "run_id": run.get("id")})

    advisory_check_names: set[str] = set()
    for entry in advisory:
        name = entry["name"]
        run = runs_by_name.get(name)
        item = {
            "name": name,
            "run_id": run.get("id") if run else None,
            "attempt": run.get("run_attempt", 1) if run else None,
            "status": run.get("status") if run else "missing",
            "conclusion": run.get("conclusion") if run else None,
            "url": run.get("html_url") if run else None,
            "jobs": [],
        }
        advisory_results.append(item)
        advisory_check_names.add(name)
        advisory_jobs = [
            *entry.get("required_jobs", []),
            *entry.get("conditional_jobs", []),
        ]
        advisory_check_names.update(
            job_entry if isinstance(job_entry, str) else job_entry["name"]
            for job_entry in advisory_jobs
        )
        if run is None:
            continue
        run_jobs = [job for job in jobs if job.get("workflow_run_id") == run.get("id")]
        for job_entry in advisory_jobs:
            job_name = job_entry if isinstance(job_entry, str) else job_entry["name"]
            job = next(
                (candidate for candidate in run_jobs if candidate.get("name") == job_name), None
            )
            item["jobs"].append(
                {
                    "name": job_name,
                    "status": job.get("status") if job else "missing",
                    "conclusion": job.get("conclusion") if job else None,
                    "url": job.get("html_url") if job else None,
                }
            )

    conditional = {
        item["name"]: set(item.get("allowed_conclusions", ["neutral", "skipped"]))
        for item in snapshot.get("conditional_checks", [])
        if isinstance(item, dict) and item.get("reason", "").strip()
    }
    accepted_conditional: list[dict[str, Any]] = []
    for check in snapshot.get("check_runs", []):
        if check.get("name") in advisory_check_names:
            continue
        conclusion = check.get("conclusion")
        if check.get("status") != "completed":
            pending.append(f"check-run: {check.get('name')}")
        elif conclusion != "success" and conclusion in conditional.get(check.get("name"), set()):
            accepted_conditional.append(
                {"name": check.get("name"), "conclusion": conclusion, "kind": "check_run"}
            )
        elif conclusion != "success":
            failed.append(
                {
                    "name": check.get("name"),
                    "conclusion": conclusion,
                    "url": check.get("html_url"),
                }
            )
    for status in snapshot.get("statuses", []):
        state = status.get("state")
        context = status.get("context", "unknown")
        if state == "pending":
            pending.append(f"status: {context}")
        elif state != "success":
            failed.append(
                {
                    "name": context,
                    "conclusion": state,
                    "url": status.get("target_url"),
                }
            )
    return {
        "required": required,
        "advisory": advisory_results,
        "missing": missing,
        "pending": pending,
        "failed": failed,
        "accepted_conditional": accepted_conditional,
    }


def _failed_run_logs(
    root: Path,
    failures: list[dict[str, Any]],
    *,
    command_runner: Any = None,
) -> list[dict[str, Any]]:
    command_runner = command_runner or _run_command
    run_ids = sorted({item.get("run_id") for item in failures if item.get("run_id")})
    return [
        command_runner(
            root,
            {
                "display": f"gh run view {run_id} --log-failed",
                "args": ["gh", "run", "view", str(run_id), "--log-failed"],
                "env": {},
            },
        )
        for run_id in run_ids
    ]


def _compact_github_result(
    snapshot: dict[str, Any],
    classified: dict[str, Any],
    *,
    discovery_duration: float,
    total_duration: float,
) -> dict[str, Any]:
    """Return stable watcher evidence without repeating complete API payloads."""
    return {
        "sha": snapshot["sha"],
        "repository": snapshot["repository"],
        "push_target": f"origin/{WORK_BRANCH}",
        "required": classified["required"],
        "advisory": classified["advisory"],
        "missing": classified["missing"],
        "pending": classified["pending"],
        "failures": classified["failed"],
        "status_contexts": [
            {
                "name": status.get("context"),
                "state": status.get("state"),
                "url": status.get("target_url"),
            }
            for status in snapshot.get("statuses", [])
        ],
        "check_runs": [
            {
                "name": check.get("name"),
                "status": check.get("status"),
                "conclusion": check.get("conclusion"),
                "url": check.get("html_url"),
            }
            for check in snapshot.get("check_runs", [])
        ],
        "conditional_skips_accepted": classified["accepted_conditional"],
        "discovery_duration_seconds": round(discovery_duration, 3),
        "total_duration_seconds": round(total_duration, 3),
    }


def _github_result_receipt(result: dict[str, Any], *, detail: str) -> dict[str, Any]:
    if detail != "summary":
        return result
    status = result.get("status")
    receipt: dict[str, Any] = {
        key: result[key]
        for key in ("sha", "repository", "push_target", "status", "total_duration_seconds")
        if key in result
    }
    if status == "pending":
        changes = result.get("changes", [])
        pending = result.get("pending", [])
        pending_receipt = {
            "sha": result.get("sha"),
            "status": status,
            "watch_id": result.get("watch_id"),
            "resume_after_seconds": result.get("resume_after_seconds"),
            "changes": [_github_change_receipt(change) for change in changes[:3]],
            "change_count": len(changes),
            "pending_required_count": len(pending),
        }
        if changes:
            pending_receipt["pending_required"] = pending[:5]
            pending_receipt["missing"] = result.get("missing", [])
        return pending_receipt
    if status == "complete":
        receipt["required"] = [
            {
                "name": workflow.get("name"),
                "conclusion": workflow.get("conclusion"),
                "url": workflow.get("url"),
            }
            for workflow in result.get("required", [])
        ]
        receipt["advisory"] = [
            {
                "name": workflow.get("name"),
                "status": workflow.get("status"),
                "conclusion": workflow.get("conclusion"),
                "url": workflow.get("url"),
            }
            for workflow in result.get("advisory", [])
        ]
        accepted = result.get("conditional_skips_accepted", [])
        if accepted:
            receipt["conditional_skips_accepted"] = accepted
        if result.get("changes"):
            receipt["changes"] = result["changes"]
        return receipt
    for key in ("failures", "failed_log_excerpts", "missing", "pending", "changes"):
        if result.get(key):
            receipt[key] = result[key]
    return receipt


def _github_change_receipt(change: dict[str, Any]) -> dict[str, Any]:
    def state_label(state: Any) -> str | None:
        if not isinstance(state, dict):
            return None
        return state.get("conclusion") or state.get("status")

    return {
        "name": change.get("name"),
        "kind": change.get("kind"),
        "before": state_label(change.get("before")),
        "after": state_label(change.get("after")),
    }


def _github_check_failure(sha: str, phase: str, message: str) -> dict[str, Any]:
    return {
        "result": {"sha": sha},
        "command_results": [],
        "blockers": [{"phase": phase, "message": message}],
    }


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
            blockers=[
                {
                    "phase": "release_merge",
                    "message": "Release merge requires a clean working tree.",
                }
            ],
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
                next_actions=[
                    "Resolve the merge blocker, then rerun release_workflow(action='merge-dev')."
                ],
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
        blockers.append(
            {"phase": "release", "message": f"Release publishing must run from {RELEASE_BRANCH}."}
        )
    if health["dirty"]:
        blockers.append(
            {"phase": "release", "message": "Release publishing requires a clean working tree."}
        )
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
