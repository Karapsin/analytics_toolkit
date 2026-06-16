#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import docs_assistant

try:  # pragma: no cover - exercised only when the agent-only MCP dependency exists.
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - normal package test env has no MCP dependency.
    FastMCP = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX_DIR = docs_assistant.DEFAULT_INDEX_DIR
VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', flags=re.MULTILINE)
PYTHON_REQUIRES_RE = re.compile(r'^requires-python\s*=\s*"([^"]+)"', flags=re.MULTILINE)
CHANGELOG_HEADING_RE = re.compile(r"^##\s+([0-9]+(?:\.[0-9]+){3})\s+-\s+(.+?)\s*$", flags=re.MULTILINE)

MODULE_DOCS = {
    "ab_utils": "agent_docs/ab_utils.md",
    "ab": "agent_docs/ab_utils.md",
    "sql": "agent_docs/sql.md",
    "excel": "agent_docs/excel.md",
    "dates": "agent_docs/dates.md",
    "date": "agent_docs/dates.md",
    "general": "agent_docs/general.md",
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
        "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_ab_utils_metrics.py",
        "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_ab_utils_format.py tests/test_ab_utils_split.py tests/test_ab_utils_parallel.py",
    ],
    "dates": [
        "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_dates.py",
    ],
    "excel": [
        "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_excel_long_format.py",
    ],
    "general": [
        "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_general_read_file.py tests/test_general_logging.py",
    ],
    "sql": [
        "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_sql_connection_config.py tests/test_sql_retries.py tests/test_sql_load_table.py",
    ],
    "agent_tools": [
        "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_rag_docs.py tests/test_agent_tools_mcp.py",
    ],
    "mcp": [
        "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_agent_tools_mcp.py",
    ],
}


def prepare_start(
    task: str,
    module: str | None = None,
    root: str = ".",
    index_dir: str = DEFAULT_INDEX_DIR,
) -> dict[str, Any]:
    """Run the mandatory repo startup workflow for coding agents."""
    root_path = _resolve_root(root)
    pull = _run_git_pull(root_path)
    if not pull["ok"]:
        return {
            "ok": False,
            "phase": "git_pull",
            "message": "git pull origin main failed; stop and report the blocker.",
            "git_pull": pull,
        }

    index = docs_assistant.build_docs_index(root=root_path, index_dir=index_dir)
    route = route_agent_context(task=task, module=module)
    return {
        "ok": True,
        "phase": "complete",
        "git_pull": pull,
        "docs_index": {
            "index_dir": str(index.index_dir),
            "file_count": index.file_count,
            "chunk_count": index.chunk_count,
        },
        "repo_health": repo_health(root=str(root_path)),
        "agent_context": route,
        "next_calls": [
            "search_docs or ask_docs for focused RAG retrieval",
            "read every required instruction file before normal inspection",
        ],
    }


def index_docs(root: str = ".", index_dir: str = DEFAULT_INDEX_DIR) -> dict[str, Any]:
    """Rebuild the local docs RAG index."""
    root_path = _resolve_root(root)
    result = docs_assistant.build_docs_index(root=root_path, index_dir=index_dir)
    return {
        "ok": True,
        "index_dir": str(result.index_dir),
        "file_count": result.file_count,
        "chunk_count": result.chunk_count,
    }


def search_docs(query: str, top_k: int = 5, index_dir: str = DEFAULT_INDEX_DIR) -> dict[str, Any]:
    """Search the local docs RAG index."""
    results = docs_assistant.search_docs(query, index_dir=index_dir, top_k=top_k)
    return {
        "ok": True,
        "results": [_search_result_to_dict(result) for result in results],
        "freshness_warnings": _freshness_warnings(index_dir),
    }


def ask_docs(query: str, top_k: int = 5, index_dir: str = DEFAULT_INDEX_DIR) -> dict[str, Any]:
    """Return a grounded no-LLM docs answer from local RAG snippets."""
    answer = docs_assistant.ask_docs(query, index_dir=index_dir, top_k=top_k, no_llm=True)
    return {
        "ok": True,
        "answer": answer.answer,
        "citations": answer.citations,
        "results": [_search_result_to_dict(result) for result in answer.results],
        "freshness_warnings": _freshness_warnings(index_dir),
    }


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
            "Use search_docs or ask_docs after prepare_start for focused RAG context.",
            "Read every required instruction file before normal repository inspection.",
        ],
        "notes": _routing_notes(required),
    }


def repo_health(root: str = ".") -> dict[str, Any]:
    """Return read-only repository status useful to coding agents."""
    root_path = _resolve_root(root)
    pyproject_text = _read_text(root_path / "pyproject.toml")
    changelog_text = _read_text(root_path / "docs" / "CHANGELOG.md")
    latest = _latest_changelog_entry(changelog_text)
    status = _run_git(root_path, ["status", "--short"])
    branch = _run_git(root_path, ["branch", "--show-current"])
    return {
        "root": str(root_path),
        "branch": branch["stdout"].strip() if branch["ok"] else "",
        "dirty": bool(status["stdout"].strip()) if status["ok"] else None,
        "status_short": status["stdout"].splitlines() if status["ok"] else [],
        "package_version": _parse_required(VERSION_RE, pyproject_text, "project version"),
        "requires_python": _parse_required(
            PYTHON_REQUIRES_RE, pyproject_text, "requires-python"
        ),
        "latest_changelog": latest,
        "ignored_local_state": [".venv/", ".rag_index/"],
    }


def next_version(current_version: str | None = None, root: str = ".") -> dict[str, Any]:
    """Compute the next four-part package version using repo carry rules."""
    if current_version is None:
        pyproject_text = _read_text(_resolve_root(root) / "pyproject.toml")
        current_version = _parse_required(VERSION_RE, pyproject_text, "project version")
    return {
        "current_version": current_version,
        "next_version": _increment_version(current_version),
    }


def changelog_status(root: str = ".") -> dict[str, Any]:
    """Report whether pyproject and the latest changelog heading align."""
    root_path = _resolve_root(root)
    pyproject_version = _parse_required(
        VERSION_RE, _read_text(root_path / "pyproject.toml"), "project version"
    )
    latest = _latest_changelog_entry(_read_text(root_path / "docs" / "CHANGELOG.md"))
    return {
        "package_version": pyproject_version,
        "latest_changelog": latest,
        "matches": latest["version"] == pyproject_version if latest else False,
    }


def recommend_tests(area: str, change_type: str = "implementation") -> dict[str, Any]:
    """Recommend focused commands for the requested area."""
    key = _normalize_area(area)
    commands = TEST_COMMANDS.get(key, [])
    if not commands:
        commands = ["PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q"]

    required_final = []
    if _normalize_area(change_type) not in {"documentation", "docs"}:
        required_final.append("release_routines/pre_commit_checks.sh")

    return {
        "area": area,
        "change_type": change_type,
        "focused_commands": commands,
        "required_final_commands": required_final,
        "notes": [
            "Do not run tests against real databases.",
            "Use fake connections, monkeypatching, and tests/conftest.py fixtures.",
        ],
    }


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
    server.tool()(index_docs)
    server.tool()(search_docs)
    server.tool()(ask_docs)
    server.tool()(route_agent_context)
    server.tool()(repo_health)
    server.tool()(next_version)
    server.tool()(changelog_status)
    server.tool()(recommend_tests)

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


def _run_git_pull(root: Path) -> dict[str, Any]:
    return _run_git(root, ["pull", "origin", "main"])


def _handle_cli_call(argv: list[str]) -> int:
    parser = _build_cli_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
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
    prepare_parser.set_defaults(
        handler=lambda args: prepare_start(
            task=args.task,
            module=args.module,
            root=args.root,
            index_dir=args.index_dir,
        )
    )

    index_parser = subparsers.add_parser("index-docs")
    index_parser.add_argument("--root", default=".")
    index_parser.add_argument("--index-dir", default=DEFAULT_INDEX_DIR)
    index_parser.set_defaults(
        handler=lambda args: index_docs(root=args.root, index_dir=args.index_dir)
    )

    search_parser = subparsers.add_parser("search-docs")
    search_parser.add_argument("query")
    search_parser.add_argument("--top-k", type=int, default=5)
    search_parser.add_argument("--index-dir", default=DEFAULT_INDEX_DIR)
    search_parser.set_defaults(
        handler=lambda args: search_docs(
            query=args.query,
            top_k=args.top_k,
            index_dir=args.index_dir,
        )
    )

    ask_parser = subparsers.add_parser("ask-docs")
    ask_parser.add_argument("query")
    ask_parser.add_argument("--top-k", type=int, default=5)
    ask_parser.add_argument("--index-dir", default=DEFAULT_INDEX_DIR)
    ask_parser.set_defaults(
        handler=lambda args: ask_docs(
            query=args.query,
            top_k=args.top_k,
            index_dir=args.index_dir,
        )
    )

    route_parser = subparsers.add_parser("route-context")
    route_parser.add_argument("--task", required=True)
    route_parser.add_argument("--module")
    route_parser.set_defaults(
        handler=lambda args: route_agent_context(task=args.task, module=args.module)
    )

    health_parser = subparsers.add_parser("repo-health")
    health_parser.add_argument("--root", default=".")
    health_parser.set_defaults(handler=lambda args: repo_health(root=args.root))

    version_parser = subparsers.add_parser("next-version")
    version_parser.add_argument("--current-version")
    version_parser.add_argument("--root", default=".")
    version_parser.set_defaults(
        handler=lambda args: next_version(
            current_version=args.current_version,
            root=args.root,
        )
    )

    changelog_parser = subparsers.add_parser("changelog-status")
    changelog_parser.add_argument("--root", default=".")
    changelog_parser.set_defaults(
        handler=lambda args: changelog_status(root=args.root)
    )

    tests_parser = subparsers.add_parser("recommend-tests")
    tests_parser.add_argument("--area", required=True)
    tests_parser.add_argument("--change-type", default="implementation")
    tests_parser.set_defaults(
        handler=lambda args: recommend_tests(
            area=args.area,
            change_type=args.change_type,
        )
    )

    return parser


def _run_git(root: Path, args: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(root),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": str(exc)}
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
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


def _freshness_warnings(index_dir: str) -> list[str]:
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
    path = Path(root)
    if not path.is_absolute() and not (path / "pyproject.toml").is_file():
        candidate = REPO_ROOT / path
        if candidate.exists():
            path = candidate
    return path.resolve()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_required(pattern: re.Pattern[str], text: str, label: str) -> str:
    match = pattern.search(text)
    if match is None:
        raise ValueError(f"Could not parse {label}")
    return match.group(1)


def _latest_changelog_entry(text: str) -> dict[str, str]:
    match = CHANGELOG_HEADING_RE.search(text)
    if match is None:
        return {}
    return {"version": match.group(1), "date": match.group(2)}


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


def _module_doc(module: str | None) -> str | None:
    if module is None:
        return None
    return MODULE_DOCS.get(_normalize_area(module))


def _normalize_area(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return {"ab": "ab_utils", "date": "dates", "docs": "documentation"}.get(
        normalized, normalized
    )


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


if __name__ == "__main__":
    raise SystemExit(main())
