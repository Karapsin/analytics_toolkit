from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from agent_tools import mcp_server


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _FakeGithubRunner:
    def __init__(
        self,
        sha: str,
        snapshots: list[dict[str, object]],
        *,
        fail_logs: bool = False,
    ) -> None:
        self.sha = sha
        self.snapshots = snapshots
        self.index = 0
        self.fail_logs = fail_logs
        self.run_endpoints: list[str] = []

    def __call__(  # noqa: PLR0911 - endpoint dispatcher for watcher tests.
        self, root: Path, command: dict[str, object]
    ) -> dict[str, object]:
        display = str(command["display"])
        current = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        if display.startswith("gh repo view"):
            return _command_result(display, "owner/repository\n")
        if "actions/runs?" in display:
            endpoint = display[len("gh api ") :]
            self.run_endpoints.append(endpoint)
            return _command_result(display, json.dumps({"workflow_runs": current["runs"]}))
        if "/jobs?" in display:
            return _command_result(display, json.dumps({"jobs": current["jobs"]}))
        if "/check-runs?" in display:
            return _command_result(display, json.dumps({"check_runs": current["check_runs"]}))
        if display.endswith("/status"):
            result = _command_result(display, json.dumps({"statuses": current["statuses"]}))
            self.index += 1
            return result
        if display.startswith("gh run view"):
            if self.fail_logs:
                return _command_result(display, "", ok=False, stderr="log unavailable")
            return _command_result(display, "failed test output")
        raise AssertionError(display)


def _command_result(
    display: str,
    stdout: str,
    *,
    ok: bool = True,
    stderr: str = "",
) -> dict[str, object]:
    return {
        "ok": ok,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": 0 if ok else 1,
        "command": display,
        "summary": stdout or stderr,
    }


def _successful_github_snapshot(conclusion: str = "success") -> dict[str, object]:
    return {
        "runs": [
            {
                "name": "tests",
                "id": 42,
                "run_attempt": 1,
                "status": "completed",
                "conclusion": conclusion,
                "html_url": "https://example.test/run/42",
            }
        ],
        "jobs": [
            {
                "workflow_run_id": 42,
                "name": "unit tests",
                "status": "completed",
                "conclusion": conclusion,
                "html_url": "https://example.test/job/7",
                "steps": [],
            }
        ],
        "check_runs": [],
        "statuses": [],
    }


def _write_watcher_manifest(root: Path, *, conditional: bool = False) -> Path:
    manifest_dir = root / ".github"
    manifest_dir.mkdir(parents=True)
    conditional_checks = []
    if conditional:
        conditional_checks.append(
            {
                "name": "nightly fault",
                "allowed_conclusions": ["neutral", "skipped"],
                "reason": "nightly only",
            }
        )
    (manifest_dir / "required-workflows.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "branches": {
                    "dev": {
                        "workflows": [
                            {
                                "name": "tests",
                                "classification": "required_push",
                                "allowed_conclusions": ["success"],
                                "required_jobs": [
                                    {
                                        "name": "unit tests",
                                        "allowed_conclusions": ["success"],
                                    }
                                ],
                            }
                        ],
                        "conditional_checks": conditional_checks,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def _successful_head(root: Path, args: list[str]) -> dict[str, object]:
    return {
        "ok": True,
        "stdout": "a" * 40 + "\n",
        "stderr": "",
        "returncode": 0,
        "command": "git " + " ".join(args),
        "summary": "ok",
    }


def _write_minimal_repo_files(root: Path, version: str = "1.3.9.13") -> Path:
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "analytics-toolkit"',
                f'version = "{version}"',
                'requires-python = ">=3.8,<3.15"',
                "dependencies = [",
                '  "requests>=2.28.2,<3",',
                "]",
                "",
                "[project.optional-dependencies]",
                'airflow = ["apache-airflow>=2.4,<3"]',
            ]
        ),
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "\n".join(
            [
                "# analytics_toolkit",
                "",
                f"**Version:** `{version}`<br>",
                "**Depends:** Python (`>=3.8,<3.15`)<br>",
                "**Imports:** [requests](https://pypi.org/project/requests/) (`>=2.28.2,<3`)<br>",
                "**Suggests:** [apache-airflow](https://pypi.org/project/apache-airflow/) (`>=2.4,<3`; optional extra `airflow`)<br>",
            ]
        ),
        encoding="utf-8",
    )
    (root / "docs").mkdir()
    (root / "docs" / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## {version} - 2026-06-16\n\n- Existing entry.\n",
        encoding="utf-8",
    )
    return root


def _write_unreleased_changelog(root: Path, summaries: list[str]) -> None:
    changelog = root / "docs" / "CHANGELOG.md"
    current = changelog.read_text(encoding="utf-8")
    bullets = "\n".join(f"- {summary.rstrip('.')}." for summary in summaries)
    changelog.write_text(
        current.replace("# Changelog\n\n", f"# Changelog\n\n## Unreleased\n\n{bullets}\n\n"),
        encoding="utf-8",
    )


def _init_git_repo(root: Path) -> None:
    (root / ".gitignore").write_text(".rag_index/\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Agent Tools Test",
        "-c",
        "user.email=agent-tools-test@example.invalid",
        "commit",
        "-m",
        "Initial test repo",
    )


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _write_docs_project(root: Path) -> Path:
    _write_minimal_repo_files(root)
    (root / "agent_docs").mkdir()
    (root / "agent_docs" / "development.md").write_text(
        "# Development\n\nUse run_checks for focused validation.\n",
        encoding="utf-8",
    )
    (root / "agent_tools").mkdir()
    (root / "agent_tools" / "README.md").write_text(
        '# Agent Tools\n\nThe MCP docs tool provides local RAG retrieval for agents.\nUse docs(query, mode="search") for snippets.',
        encoding="utf-8",
    )
    return root


def _write_changed_version_metadata(root: Path, version: str) -> None:
    pyproject = root / "pyproject.toml"
    readme = root / "README.md"
    changelog = root / "docs" / "CHANGELOG.md"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            'version = "1.3.9.13"', f'version = "{version}"'
        ),
        encoding="utf-8",
    )
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(
            "**Version:** `1.3.9.13`", f"**Version:** `{version}`"
        ),
        encoding="utf-8",
    )
    changelog.write_text(
        f"# Changelog\n\n## {version} - 2026-06-16\n\n- Updated workflow.\n\n"
        + changelog.read_text(encoding="utf-8"),
        encoding="utf-8",
    )


__all__ = [
    "Any",
    "Path",
    "_FakeClock",
    "_FakeGithubRunner",
    "_command_result",
    "_git",
    "_init_git_repo",
    "_successful_github_snapshot",
    "_successful_head",
    "_write_changed_version_metadata",
    "_write_docs_project",
    "_write_minimal_repo_files",
    "_write_unreleased_changelog",
    "_write_watcher_manifest",
    "json",
    "mcp_server",
    "pytest",
    "subprocess",
    "sys",
    "types",
]
