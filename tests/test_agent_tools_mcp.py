from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_tools"))

import mcp_server


def test_increment_version_carries_four_part_versions() -> None:
    assert mcp_server._increment_version("1.3.6.6") == "1.3.6.7"
    assert mcp_server._increment_version("1.3.6.19") == "1.3.7.0"
    assert mcp_server._increment_version("1.3.19.19") == "1.4.0.0"
    assert mcp_server._increment_version("1.19.19.19") == "2.0.0.0"


@pytest.mark.parametrize("version", ["1.2.3", "1.2.3.20", "1.2.-1.0"])
def test_increment_version_rejects_invalid_versions(version: str) -> None:
    with pytest.raises(ValueError):
        mcp_server._increment_version(version)


def test_create_mcp_server_exposes_only_consolidated_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMCP:
        def __init__(self, name: str) -> None:
            self.name = name
            self.tools: list[str] = []
            self.resources: list[str] = []

        def tool(self):
            def register(func):
                self.tools.append(func.__name__)
                return func

            return register

        def resource(self, uri: str):
            def register(func):
                self.resources.append(uri)
                return func

            return register

    created: list[FakeMCP] = []

    def fake_factory(name: str) -> FakeMCP:
        server = FakeMCP(name)
        created.append(server)
        return server

    monkeypatch.setattr(mcp_server, "FastMCP", fake_factory)

    mcp_server.create_mcp_server()

    assert created[0].tools == [
        "prepare_start",
        "docs",
        "workflow_status",
        "workflow_metrics",
        "change_impact",
        "version_bump",
        "run_checks",
        "git_workflow",
        "release_workflow",
    ]


def test_resolve_root_defaults_to_repo_root_even_when_cwd_has_pyproject(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    monkeypatch.setattr(mcp_server, "REPO_ROOT", repo_root)
    monkeypatch.chdir(outside)

    assert mcp_server._resolve_root(".") == repo_root.resolve()
    assert mcp_server._resolve_root("") == repo_root.resolve()
    assert mcp_server._resolve_root("nested") == (repo_root / "nested").resolve()
    assert mcp_server._resolve_root(str(outside)) == outside.resolve()


def test_prepare_start_stops_on_failed_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    calls: list[str] = []

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        calls.append(str(command["display"]))
        return {
            "ok": False,
            "command": command["display"],
            "returncode": 1,
            "stdout": "",
            "stderr": "network unavailable",
            "summary": "network unavailable",
        }

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)

    result = mcp_server.prepare_start("implementation", root=str(root))

    assert result["ok"] is False
    assert result["tool"] == "prepare_start"
    assert result["result"]["phase"] == "git_fetch_dev"
    assert result["blockers"][0]["excerpt"] == "network unavailable"
    assert calls == ["git fetch origin dev"]


def test_prepare_start_sequences_environment_and_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    commands: list[str] = []

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        commands.append(str(command["display"]))
        return {
            "ok": True,
            "command": command["display"],
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "summary": "ok",
        }

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)
    monkeypatch.setattr(
        mcp_server.docs_assistant,
        "build_docs_index",
        lambda root, index_dir: types.SimpleNamespace(
            index_dir=Path(root) / index_dir,
            file_count=3,
            chunk_count=5,
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "repo_health",
        lambda root: {"branch": "dev"},
    )
    monkeypatch.setattr(
        mcp_server,
        "workflow_status",
        lambda **kwargs: {
            "result": {
                "repo_health": {"branch": "dev"},
                "required_instruction_files": ["AGENTS.md", "agent_docs/development.md"],
                "metadata_status": {"ok": True},
                "recommended_checks": {"focused_commands": []},
            }
        },
    )

    result = mcp_server.prepare_start("implementation", root=str(root))

    assert result["ok"] is True, result
    assert commands == [
        "git fetch origin dev",
        "git switch dev",
        "git pull --ff-only origin dev",
        "python -m venv .venv",
        ".venv/bin/python -m pip install -r agent_tools/requirements-mcp.txt",
        ".venv/bin/python -m pip install -e . pytest tox",
    ]
    assert result["result"]["docs_index"]["chunk_count"] == 5


def test_prepare_start_fails_when_branch_verification_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        return {
            "ok": True,
            "command": command["display"],
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "summary": "ok",
        }

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)
    monkeypatch.setattr(
        mcp_server,
        "repo_health",
        lambda root: {"branch": "main"},
    )

    result = mcp_server.prepare_start("implementation", root=str(root))

    assert result["ok"] is False
    assert result["result"]["phase"] == "branch_verify"
    assert result["blockers"][0]["expected_branch"] == "dev"


def test_prepare_start_reuses_matching_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    (root / "agent_tools").mkdir()
    (root / "agent_tools" / "requirements-mcp.txt").write_text("mcp>=1\n", encoding="utf-8")
    (root / "tox.ini").write_text("[tox]\n", encoding="utf-8")
    venv_python = root / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    fingerprint = mcp_server._environment_fingerprint(root, True)
    mcp_server._write_environment_state(
        root,
        fingerprint=fingerprint,
        ensure_project_env=True,
    )
    commands: list[str] = []

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        commands.append(str(command["display"]))
        return _command_result(str(command["display"]), "ok")

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)
    monkeypatch.setattr(mcp_server, "repo_health", lambda root: {"branch": "dev"})
    monkeypatch.setattr(
        mcp_server.docs_assistant,
        "build_docs_index",
        lambda root, index_dir: types.SimpleNamespace(
            index_dir=Path(root) / index_dir,
            file_count=1,
            chunk_count=1,
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "workflow_status",
        lambda **kwargs: {
            "result": {
                "repo_health": {"branch": "dev"},
                "required_instruction_files": ["AGENTS.md"],
                "metadata_status": {"ok": True},
                "recommended_checks": {},
            }
        },
    )

    result = mcp_server.prepare_start("implementation", root=str(root))

    assert result["ok"] is True
    assert result["result"]["environment"]["reused"] is True
    assert ".venv/bin/python agent environment health check" in commands
    assert not any("pip install" in command for command in commands)
    assert result["telemetry"]["response_bytes"] <= 2_500
    assert result["telemetry"]["within_budget"] is True


def test_docs_search_and_ask_modes(tmp_path: Path) -> None:
    root = _write_docs_project(tmp_path / "project")
    index_dir = tmp_path / "rag-index"
    mcp_server.docs_assistant.build_docs_index(root=root, index_dir=index_dir)

    search_result = mcp_server.docs(
        "docs MCP workflow",
        mode="search",
        top_k=1,
        index_dir=str(index_dir),
    )
    ask_result = mcp_server.docs(
        "docs MCP workflow",
        mode="ask",
        top_k=1,
        index_dir=str(index_dir),
    )

    assert search_result["ok"] is True
    assert search_result["tool"] == "docs"
    assert set(search_result["result"]["results"][0]) == {
        "citation",
        "heading",
        "snippet",
    }
    assert ask_result["result"]["answer"].startswith("Most relevant passages")
    assert ask_result["result"]["citations"][0].startswith("agent_tools/README.md:L")
    assert "results" not in ask_result["result"]
    assert search_result["telemetry"]["response_bytes"] <= 3_500


def test_docs_summary_deduplicates_results_and_reports_counts(tmp_path: Path) -> None:
    root = _write_docs_project(tmp_path / "project")
    index_dir = tmp_path / "rag-index"
    mcp_server.docs_assistant.build_docs_index(root=root, index_dir=index_dir)

    result = mcp_server.docs(
        "docs MCP workflow",
        top_k=3,
        index_dir=str(index_dir),
    )

    keys = [(item["citation"], item["heading"]) for item in result["result"]["results"]]
    assert len(keys) == len(set(keys))
    assert result["result"]["returned_count"] == len(keys)
    assert result["result"]["total_count"] >= len(keys)


def test_docs_default_index_dir_is_resolved_from_repo_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_docs_project(tmp_path / "project")
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(mcp_server, "REPO_ROOT", root)
    monkeypatch.chdir(outside)
    mcp_server.docs_assistant.build_docs_index(root=root, index_dir=root / ".rag_index")

    result = mcp_server.docs("docs MCP workflow", mode="search", top_k=1)

    assert result["ok"] is True
    assert "index_dir" not in result["input"]
    assert "resolved_index_dir" not in result["input"]
    assert result["result"]["results"][0]["citation"].startswith("agent_tools/README.md:L")


def test_workflow_status_combines_routing_health_metadata_and_checks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")

    def fake_run_git(root_path: Path, args: list[str]) -> dict[str, object]:
        stdout_by_args = {
            ("branch", "--show-current"): "main\n",
            ("status", "--short"): "",
            ("diff", "--stat"): " agent_tools/mcp_server.py | 2 ++\n",
            ("diff", "--cached", "--stat"): " tests/test_agent_tools_mcp.py | 3 +++\n",
        }
        return {
            "ok": True,
            "stdout": stdout_by_args.get(tuple(args), ""),
            "stderr": "",
            "returncode": 0,
            "command": "git",
            "summary": "",
        }

    monkeypatch.setattr(mcp_server, "_run_git", fake_run_git)

    result = mcp_server.workflow_status(
        "implementation release workflow",
        module="agent_tools",
        instructions_read=True,
        root=str(root),
        detail="diagnostic",
    )

    assert result["ok"] is True
    assert result["tool"] == "workflow_status"
    assert result["result"]["repo_health"]["branch"] == "main"
    assert result["result"]["repo_health"]["dirty"] is False
    assert result["result"]["repo_health"]["status_short"] == []
    assert result["result"]["repo_health"]["diff_stat"] == [" agent_tools/mcp_server.py | 2 ++"]
    assert result["result"]["repo_health"]["staged_diff_stat"] == [
        " tests/test_agent_tools_mcp.py | 3 +++"
    ]
    assert "agent_docs/development.md" in result["result"]["required_instruction_files"]
    assert "agent_docs/release.md" in result["result"]["required_instruction_files"]
    assert "agent_tools/README.md" in result["result"]["required_instruction_files"]
    assert result["result"]["metadata_status"]["ok"] is True
    assert result["result"]["recommended_checks"]["focused_commands"] == [
        "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_rag_docs.py tests/test_agent_tools_mcp.py tests/test_required_workflows.py"
    ]


def test_change_impact_reports_sql_contract_and_architecture_headroom() -> None:
    result = mcp_server.change_impact(
        "add an option to sql.read",
        module="sql",
        symbols=["sql.read"],
        root=str(mcp_server.REPO_ROOT),
    )

    assert result["ok"] is True
    assert "agent_docs/development.md" in result["result"]["required_instruction_files"]
    contract = result["result"]["public_contracts"][0]
    output_type = next(item for item in contract["parameters"] if item["name"] == "output_type")
    assert output_type["pointer"] == "/exports/read/parameters/output_type"
    assert output_type["manifest_status"] == "aligned"
    budgets = {item["path"]: item for item in result["result"]["architecture"]["modules"]}
    assert budgets["analytics_toolkit/sql/backends/base.py"]["remaining_lines"] == 5
    assert "docs/modules/sql/functions/read.md" in result["result"]["documentation_paths"]
    assert result["telemetry"]["response_bytes"] < 8_000


def test_repo_health_reports_unstaged_and_staged_diff_stats(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    calls: list[list[str]] = []

    def fake_run_git(root_path: Path, args: list[str]) -> dict[str, object]:
        calls.append(args)
        stdout_by_args = {
            ("status", "--short"): " M agent_tools/mcp_server.py\n",
            ("branch", "--show-current"): "dev\n",
            ("diff", "--stat"): " agent_tools/mcp_server.py | 2 ++\n",
            ("diff", "--cached", "--stat"): " tests/test_agent_tools_mcp.py | 3 +++\n",
        }
        return {
            "ok": True,
            "stdout": stdout_by_args[tuple(args)],
            "stderr": "",
            "returncode": 0,
            "command": "git " + " ".join(args),
            "summary": "",
        }

    monkeypatch.setattr(mcp_server, "_run_git", fake_run_git)

    result = mcp_server.repo_health(root=str(root))

    assert calls == [
        ["status", "--short"],
        ["branch", "--show-current"],
        ["diff", "--stat"],
        ["diff", "--cached", "--stat"],
    ]
    assert result["branch"] == "dev"
    assert result["dirty"] is True
    assert result["status_short"] == [" M agent_tools/mcp_server.py"]
    assert result["diff_stat"] == [" agent_tools/mcp_server.py | 2 ++"]
    assert result["staged_diff_stat"] == [" tests/test_agent_tools_mcp.py | 3 +++"]


def test_workflow_status_suppresses_instruction_reminder_when_confirmed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    monkeypatch.setattr(
        mcp_server,
        "_run_git",
        lambda root_path, args: {
            "ok": True,
            "stdout": "main\n" if args == ["branch", "--show-current"] else "",
            "stderr": "",
            "returncode": 0,
            "command": "git",
            "summary": "",
        },
    )

    result = mcp_server.workflow_status(
        "implementation",
        module="agent_tools",
        instructions_read=True,
        root=str(root),
    )

    assert result["ok"] is True
    assert result["result"]["missing_mandatory_actions"] == []
    repeated = mcp_server.workflow_status(
        "implementation",
        module="agent_tools",
        instructions_read=True,
        root=str(root),
    )
    assert repeated["result"]["startup_context"]["reused"] is True
    assert repeated["result"]["required_instruction_files"] == []
    assert repeated["telemetry"]["response_bytes"] <= 2_500


def test_workflow_status_not_ok_when_mandatory_actions_are_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    monkeypatch.setattr(
        mcp_server,
        "_run_git",
        lambda root_path, args: {
            "ok": True,
            "stdout": "main\n" if args == ["branch", "--show-current"] else "",
            "stderr": "",
            "returncode": 0,
            "command": "git",
            "summary": "",
        },
    )

    result = mcp_server.workflow_status(
        "implementation",
        module="agent_tools",
        instructions_read=False,
        root=str(root),
    )

    assert result["ok"] is False
    assert result["blockers"] == []
    assert result["result"]["missing_mandatory_actions"] == [
        "Read required instruction files: AGENTS.md, agent_docs/development.md, agent_tools/README.md"
    ]
    assert result["next_actions"] == result["result"]["missing_mandatory_actions"]


def test_workflow_status_cli_accepts_instructions_read_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_workflow_status(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "workflow_status", fake_workflow_status)
    parser = mcp_server._build_cli_parser()
    args = parser.parse_args(
        [
            "workflow-status",
            "--task",
            "implementation",
            "--module",
            "agent_tools",
            "--instructions-read",
        ]
    )

    result = args.handler(args)

    assert result == {"ok": True}
    assert captured["instructions_read"] is True


def test_version_bump_updates_unreleased_below_threshold(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project", version="1.3.9.13")

    dry_run = mcp_server.version_bump(
        "Consolidated agent MCP workflow", root=str(root), dry_run=True
    )
    applied = mcp_server.version_bump("Consolidated agent MCP workflow", root=str(root))

    assert dry_run["result"]["decision"] == "unreleased"
    assert dry_run["result"]["planned_version"] is None
    assert applied["result"]["decision"] == "unreleased"
    assert 'version = "1.3.9.13"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "**Version:** `1.3.9.13`" in (root / "README.md").read_text(encoding="utf-8")
    changelog = (root / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## Unreleased" in changelog
    assert "- Consolidated agent MCP workflow." in changelog
    assert "## 1.3.9.14 - " not in changelog
    assert applied["ok"] is True


def test_version_bump_releases_tenth_unreleased_bullet(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project", version="1.3.9.13")
    _write_unreleased_changelog(root, [f"Existing change {index}" for index in range(1, 10)])
    changelog_path = root / "docs" / "CHANGELOG.md"
    changelog_path.write_text(
        changelog_path.read_text(encoding="utf-8").replace(
            "- Existing change 1.",
            "- Existing change 1 with wrapped\n  continuation text.",
        ),
        encoding="utf-8",
    )

    assert (
        mcp_server._count_unreleased_changelog_bullets(changelog_path.read_text(encoding="utf-8"))
        == 9
    )

    dry_run = mcp_server.version_bump("Tenth change", root=str(root), dry_run=True)
    applied = mcp_server.version_bump("Tenth change", root=str(root))

    assert dry_run["result"]["decision"] == "bump"
    assert dry_run["result"]["planned_version"] == "1.3.9.14"
    assert 'version = "1.3.9.14"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "**Version:** `1.3.9.14`" in (root / "README.md").read_text(encoding="utf-8")
    changelog = (root / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## Unreleased" not in changelog
    assert "## 1.3.9.14 - " in changelog
    assert "- Existing change 1 with wrapped\n  continuation text." in changelog
    assert "- Tenth change." in changelog
    assert applied["ok"] is True


def test_version_bump_force_releases_below_threshold(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project", version="1.3.9.13")
    _write_unreleased_changelog(root, ["Existing change 1", "Existing change 2"])
    changelog_path = root / "docs" / "CHANGELOG.md"
    changelog_path.write_text(
        changelog_path.read_text(encoding="utf-8").replace(
            "- Existing change 1.",
            "- Existing change 1 with wrapped\n  continuation text.",
        ),
        encoding="utf-8",
    )

    dry_run = mcp_server.version_bump(
        change_type="release",
        force_release=True,
        root=str(root),
        dry_run=True,
    )
    applied = mcp_server.version_bump(
        change_type="release",
        force_release=True,
        root=str(root),
    )

    assert dry_run["result"]["decision"] == "bump"
    assert dry_run["result"]["planned_version"] == "1.3.9.14"
    assert dry_run["result"]["unreleased_count"] == 2
    assert applied["result"]["decision"] == "bump"
    assert 'version = "1.3.9.14"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    changelog = (root / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## Unreleased" not in changelog
    assert "- Existing change 1 with wrapped\n  continuation text." in changelog
    assert "- Existing change 2." in changelog


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"change_type": "implementation", "force_release": True},
            "force_release requires a release-oriented change_type",
        ),
        (
            {"summary": "Release summary", "change_type": "release", "force_release": True},
            "omit summary when force_release is enabled",
        ),
    ],
)
def test_version_bump_rejects_invalid_force_release_options(
    tmp_path: Path,
    kwargs: dict[str, object],
    message: str,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    _write_unreleased_changelog(root, ["Existing change"])

    result = mcp_server.version_bump(root=str(root), **kwargs)

    assert result["ok"] is False
    assert result["blockers"][0]["message"] == message


def test_version_bump_force_release_requires_unreleased_entries(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")

    result = mcp_server.version_bump(
        change_type="release",
        force_release=True,
        root=str(root),
    )

    assert result["ok"] is False
    assert result["blockers"][0]["message"] == (
        "no unreleased changelog entries are available to release"
    )


def test_version_bump_fails_when_readme_version_marker_is_missing_at_threshold(
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project", version="1.3.9.13")
    _write_unreleased_changelog(root, [f"Existing change {index}" for index in range(1, 10)])
    (root / "README.md").write_text("# analytics_toolkit\n", encoding="utf-8")

    result = mcp_server.version_bump("Consolidated agent MCP workflow", root=str(root))

    assert result["ok"] is False
    assert result["blockers"][0]["message"] == "Could not update README version"
    assert 'version = "1.3.9.13"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "## 1.3.9.14 - " not in (root / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")


def test_version_bump_does_not_partially_write_when_release_changelog_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project", version="1.3.9.13")
    _write_unreleased_changelog(root, [f"Existing change {index}" for index in range(1, 10)])
    original_pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    original_readme = (root / "README.md").read_text(encoding="utf-8")

    def fail_changelog(text: str, entry: str) -> str:
        msg = "Could not update changelog"
        raise ValueError(msg)

    monkeypatch.setattr(mcp_server, "_release_unreleased_changelog_text", fail_changelog)

    result = mcp_server.version_bump("Consolidated agent MCP workflow", root=str(root))

    assert result["ok"] is False
    assert (root / "pyproject.toml").read_text(encoding="utf-8") == original_pyproject
    assert (root / "README.md").read_text(encoding="utf-8") == original_readme


def test_version_bump_skips_documentation_only_changes(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")

    result = mcp_server.version_bump(
        "Updated docs",
        change_type="documentation",
        root=str(root),
    )

    assert result["result"]["decision"] == "no_bump"
    assert 'version = "1.3.9.13"' in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_run_checks_dry_run_and_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")

    dry_run = mcp_server.run_checks(
        area="agent_tools", level="focused", root=str(root), dry_run=True
    )
    assert dry_run["result"]["planned_commands"] == [
        "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_rag_docs.py tests/test_agent_tools_mcp.py tests/test_required_workflows.py"
    ]

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        return {
            "ok": False,
            "command": command["display"],
            "returncode": 2,
            "stdout": "",
            "stderr": "failed",
            "summary": "failed",
        }

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)

    failed = mcp_server.run_checks(area="agent_tools", level="focused", root=str(root))

    assert failed["ok"] is False
    assert failed["blockers"][0]["returncode"] == 2


def test_tool_output_compacts_large_command_output_and_supports_full_detail() -> None:
    command = _command_result("large command", "x" * 50_000)

    compact = mcp_server._tool_output(
        "run_checks",
        {"detail": "summary"},
        command_results=[command],
    )
    full = mcp_server._tool_output(
        "run_checks",
        {"detail": "full"},
        command_results=[command],
    )

    diagnostic = mcp_server._tool_output(
        "run_checks",
        {"detail": "diagnostic"},
        command_results=[command],
    )

    assert compact["command_results"] == []
    assert "raw_output_bytes" not in compact["telemetry"]
    assert diagnostic["telemetry"]["raw_output_bytes"] == 50_000
    assert diagnostic["telemetry"]["suppressed_output_bytes"] > 49_000
    assert len(json.dumps(compact)) < 2_000
    assert "stdout_excerpt" not in diagnostic["command_results"][0]
    assert "stderr_excerpt" not in diagnostic["command_results"][0]
    assert full["command_results"][0]["stdout"] == "x" * 50_000
    assert compact["telemetry"]["response_bytes"] <= (full["telemetry"]["response_bytes"] * 0.3)


def test_summary_response_budget_is_enforced() -> None:
    result = mcp_server._tool_output(
        "run_checks",
        {"detail": "summary"},
        result={"items": ["x" * 2_000 for _ in range(20)]},
        blockers=[{"phase": "test", "message": "y" * 5_000}],
    )

    assert result["telemetry"]["truncated"] is True
    assert result["telemetry"]["within_budget"] is True
    assert result["telemetry"]["response_bytes"] <= 2_500


def test_workflow_metrics_aggregate_active_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    root = tmp_path / "project"
    state_dir = root / ".rag_index"
    state_dir.mkdir(parents=True)
    (state_dir / "startup_context.json").write_text(
        json.dumps({"id": "session-1"}),
        encoding="utf-8",
    )

    mcp_server._tool_output(
        "run_checks",
        {"root": str(root), "detail": "summary"},
        result={"failure_signature": "repeat"},
        ok=False,
    )
    mcp_server._tool_output(
        "run_checks",
        {"root": str(root), "detail": "summary"},
        result={"failure_signature": "repeat"},
        ok=False,
    )
    mcp_server._tool_output(
        "workflow_status",
        {"root": str(root), "detail": "summary"},
    )

    metrics = mcp_server.workflow_metrics(root=str(root))

    assert metrics["result"]["session_id"] == "session-1"
    assert metrics["result"]["call_count"] == 3
    assert metrics["result"]["repeated_failure_count"] == 1
    assert metrics["result"]["estimated_response_tokens"] > 0
    assert "model tokens unavailable" in metrics["result"]["token_estimate_method"]


def test_run_command_persists_private_full_log(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    result = mcp_server._run_command(
        root,
        {
            "display": "emit output",
            "args": [sys.executable, "-c", "print('full diagnostic')"],
            "env": {},
        },
    )

    log_path = root / result["log_ref"]
    assert "full diagnostic" in log_path.read_text(encoding="utf-8")
    assert log_path.stat().st_mode & 0o777 == 0o600


def test_run_checks_bounds_large_failure_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    monkeypatch.setattr(
        mcp_server,
        "_run_command",
        lambda root_path, command: _command_result(
            str(command["display"]), "failure\n" + "x" * 100_000, ok=False
        ),
    )

    result = mcp_server.run_checks(area="agent_tools", root=str(root))

    assert result["ok"] is False
    assert len(json.dumps(result)) < 16_000
    assert result["telemetry"]["response_bytes"] <= 4_000
    assert result["telemetry"]["within_budget"] is True


def test_diagnostic_failure_returns_one_bounded_evidence_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    monkeypatch.setattr(
        mcp_server,
        "_run_command",
        lambda root_path, command: _command_result(
            str(command["display"]), "failure marker\n" + "x" * 50_000, ok=False
        ),
    )

    result = mcp_server.run_checks(
        area="agent_tools",
        root=str(root),
        detail="diagnostic",
    )

    assert "excerpt" in result["blockers"][0]
    assert "stdout_excerpt" not in result["command_results"][0]
    assert "stderr_excerpt" not in result["command_results"][0]
    assert result["telemetry"]["response_bytes"] <= 6_000
    assert result["telemetry"]["within_budget"] is True


def test_command_blocker_ignores_stage_only_stderr_for_actionable_excerpt() -> None:
    result = _command_result(
        "validation",
        "FAILED tests/test_example.py::test_case - assertion",
        ok=False,
        stderr="::agent-check-stage::pytest::end::failed",
    )

    blocker = mcp_server._command_blocker("run_checks", result)

    assert blocker["excerpt"].startswith("FAILED tests/test_example.py::test_case")


def test_precommit_checks_stop_before_later_stages_when_static_gate_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    commands: list[str] = []

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        display = str(command["display"])
        commands.append(display)
        return _command_result(display, "lint failed", ok=False)

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)
    monkeypatch.setattr(mcp_server, "_working_tree_fingerprint", lambda root_path: "tree")
    monkeypatch.setattr(mcp_server, "_precommit_toolchain_fingerprint", lambda root_path: "tools")

    result = mcp_server.run_checks(level="precommit", root=str(root))

    assert commands == ["release_routines/pre_commit_checks.sh --static"]
    assert result["result"]["level"] == "precommit"
    assert result["result"]["command_count"] == 4
    assert result["result"]["failed_command_index"] == 0
    assert result["result"]["stages"][0]["status"] == "failed"
    assert result["result"]["failure_changed"] is True
    assert result["result"]["failure_signature"]


def test_precommit_resumes_successful_stages_for_identical_tree_and_toolchain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    commands: list[str] = []
    fail_artifacts = True

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        nonlocal fail_artifacts
        display = str(command["display"])
        commands.append(display)
        if display.endswith("--artifacts") and fail_artifacts:
            fail_artifacts = False
            return _command_result(display, "artifact failure", ok=False)
        return _command_result(display, "passed")

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)
    monkeypatch.setattr(mcp_server, "_working_tree_fingerprint", lambda root_path: "tree")
    monkeypatch.setattr(mcp_server, "_precommit_toolchain_fingerprint", lambda root_path: "tools")

    first = mcp_server.run_checks(level="precommit", root=str(root))
    first_commands = list(commands)
    commands.clear()
    second = mcp_server.run_checks(level="precommit", root=str(root))

    assert first["ok"] is False
    assert first_commands == [
        "release_routines/pre_commit_checks.sh --static",
        "release_routines/pre_commit_checks.sh --coverage",
        "release_routines/pre_commit_checks.sh --artifacts",
    ]
    assert commands == [
        "release_routines/pre_commit_checks.sh --artifacts",
        "release_routines/pre_commit_checks.sh --matrix",
    ]
    assert second["ok"] is True
    assert second["result"]["reused_stage_count"] == 2
    assert second["result"]["executed_stage_count"] == 2
    assert [stage["status"] for stage in second["result"]["stages"]] == [
        "reused",
        "reused",
        "executed",
        "executed",
    ]


def test_precommit_stage_receipt_requires_current_tree_toolchain_command_and_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = mcp_server.PRECOMMIT_CHECK_COMMANDS[0]
    now = 1_000_000.0
    monkeypatch.setattr(mcp_server.time, "time", lambda: now)
    receipt = {
        "fingerprint": "tree",
        "toolchain_fingerprint": "tools",
        "command_fingerprint": mcp_server._precommit_command_fingerprint(command),
        "completed_at": now - 60,
    }

    assert mcp_server._precommit_stage_receipt_is_current(
        receipt,
        fingerprint="tree",
        toolchain_fingerprint="tools",
        command=command,
    )
    assert not mcp_server._precommit_stage_receipt_is_current(
        receipt,
        fingerprint="changed-tree",
        toolchain_fingerprint="tools",
        command=command,
    )
    assert not mcp_server._precommit_stage_receipt_is_current(
        receipt,
        fingerprint="tree",
        toolchain_fingerprint="changed-tools",
        command=command,
    )
    changed_command = {**command, "args": [*command["args"], "--changed"]}
    assert not mcp_server._precommit_stage_receipt_is_current(
        receipt,
        fingerprint="tree",
        toolchain_fingerprint="tools",
        command=changed_command,
    )
    receipt["completed_at"] = now - mcp_server.PRECOMMIT_STAGE_TTL_SECONDS - 1
    assert not mcp_server._precommit_stage_receipt_is_current(
        receipt,
        fingerprint="tree",
        toolchain_fingerprint="tools",
        command=command,
    )


def test_run_checks_reports_stage_nodes_and_coverage_ratchet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    output = "\n".join(
        [
            "::agent-check-stage::tox::start::running",
            "FAILED tests/test_example.py::test_case - AssertionError",
            "Coverage targets raised; review and rerun:",
            "overall branch: 90.00% -> 91.00% covered=91/100 missing=9 prefix=overall",
            "::agent-check-stage::tox::end::failed",
        ]
    )
    monkeypatch.setattr(
        mcp_server,
        "_run_command",
        lambda root_path, command: _command_result(str(command["display"]), output, ok=False),
    )

    result = mcp_server.run_checks(area="agent_tools", root=str(root))

    blocker = result["blockers"][0]
    assert blocker["phase"] == "coverage_ratchet_confirmation"
    assert blocker["stage"] == "tox"
    assert blocker["failed_stages"] == ["tox"]
    assert blocker["test_node_ids"] == ["tests/test_example.py::test_case"]
    assert blocker["target_changes"][0].startswith("overall branch:")
    assert "Review the monotonic coverage target changes" in result["next_actions"][0]


def test_repeated_check_failure_returns_unchanged_compact_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    monkeypatch.setattr(
        mcp_server,
        "_run_command",
        lambda root_path, command: _command_result(
            str(command["display"]),
            "::agent-check-stage::pytest::end::failed\n"
            "FAILED tests/test_example.py::test_case - AssertionError",
            ok=False,
        ),
    )

    first = mcp_server.run_checks(area="agent_tools", root=str(root))
    second = mcp_server.run_checks(area="agent_tools", root=str(root))

    assert first["result"]["failure_changed"] is True
    assert second["result"]["failure_changed"] is False
    assert second["blockers"][0]["unchanged"] is True
    assert "excerpt" not in second["blockers"][0]


def test_run_checks_reports_managed_coverage_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    output = (
        "Coverage targets raised; managed update accepted:\n"
        "overall branch: 90.00% -> 91.00% covered=91/100 missing=9 prefix=overall\n"
    )
    monkeypatch.setattr(
        mcp_server,
        "_run_command",
        lambda root_path, command: _command_result(str(command["display"]), output),
    )

    result = mcp_server.run_checks(area="agent_tools", root=str(root))

    assert result["result"]["coverage_target_changes"][0].startswith("overall branch:")


def test_sql_focused_checks_include_all_sql_unit_modules() -> None:
    command = mcp_server._check_commands(
        area="sql",
        change_type="implementation",
        level="focused",
        root=mcp_server.REPO_ROOT,
    )[0]

    assert "tests/test_sql_read_outputs.py" in command["args"]
    assert "tests/test_sql_architecture.py" in command["args"]


def test_run_checks_plans_sql_integration_and_rejects_other_areas(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")

    planned = mcp_server.run_checks(
        area="sql",
        level="integration",
        root=str(root),
        dry_run=True,
    )
    rejected = mcp_server.run_checks(
        area="general",
        level="integration",
        root=str(root),
        dry_run=True,
    )

    assert planned["result"]["planned_commands"] == [
        "python -m release_routines.sql_integration --profile all --clickhouse-driver both"
    ]
    assert rejected["ok"] is False
    assert rejected["blockers"][0]["message"] == (
        "level='integration' is only supported for area='sql'"
    )


def test_run_checks_cli_accepts_integration_level(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_checks(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "run_checks", fake_run_checks)
    parser = mcp_server._build_cli_parser()
    args = parser.parse_args(["run-checks", "--area", "sql", "--level", "integration"])

    assert args.handler(args) == {"ok": True}
    assert captured["area"] == "sql"
    assert captured["level"] == "integration"
    assert captured["integration_profile"] == "all"
    assert captured["integration_clickhouse_driver"] == "both"


def test_cli_call_returns_nonzero_for_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = mcp_server._build_cli_parser()
    args = parser.parse_args(["run-checks", "--area", "general", "--level", "integration"])
    monkeypatch.setattr(mcp_server, "_build_cli_parser", lambda: parser)
    monkeypatch.setattr(parser, "parse_args", lambda _argv: args)

    assert mcp_server._handle_cli_call([]) == 1


def test_git_workflow_enforces_precommit_for_commit(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")

    result = mcp_server.git_workflow(
        "commit",
        message="Update workflow",
        paths=["docs/guide.md"],
        root=str(root),
    )

    assert result["ok"] is False
    assert result["blockers"][0]["phase"] == "precommit"


def test_git_workflow_commit_requires_explicit_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    monkeypatch.setattr(
        mcp_server,
        "_verify_precommit_success",
        lambda root_path: {"ok": True, "message": "ok"},
    )

    result = mcp_server.git_workflow("commit", message="Update workflow", root=str(root))

    assert result["ok"] is False
    assert result["blockers"][0]["phase"] == "stage"
    assert "paths are required" in result["blockers"][0]["message"]


@pytest.mark.parametrize(
    ("unsafe_path", "message"),
    [
        (".connections", "sensitive local paths"),
        (".connections/dev.toml", "sensitive local paths"),
        (".env", "sensitive local paths"),
        (".env/local", "sensitive local paths"),
        (".env.local", "sensitive local paths"),
        ("config/.env.production", "sensitive local paths"),
        (".certs/client.key", "sensitive local paths"),
        (str(Path("/tmp/outside.txt")), "absolute paths"),
        ("../outside.txt", "paths must not escape"),
        (".", "repository root"),
        (":(glob)*.py", "pathspec magic"),
        ("agent_tools/*.py", "glob-style pathspecs"),
    ],
)
def test_git_workflow_blocks_unsafe_commit_paths(
    unsafe_path: str,
    message: str,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")

    result = mcp_server.git_workflow(
        "commit",
        message="Update workflow",
        paths=[unsafe_path],
        root=str(root),
    )

    assert result["ok"] is False
    assert result["blockers"][0]["phase"] == "stage"
    assert message in result["blockers"][0]["message"]


def test_commit_path_validation_allows_normal_repo_file(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    agent_tools_dir = root / "agent_tools"
    agent_tools_dir.mkdir()
    (agent_tools_dir / "mcp_server.py").write_text("# tool\n", encoding="utf-8")

    result = mcp_server._validated_commit_paths(
        root,
        ["agent_tools/mcp_server.py"],
    )

    assert result == {"paths": ["agent_tools/mcp_server.py"], "blockers": []}


def test_workflow_status_requires_version_bump_for_dirty_implementation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    (root / "agent_tools").mkdir()
    (root / "agent_tools" / "mcp_server.py").write_text("# initial\n", encoding="utf-8")
    _init_git_repo(root)
    (root / "agent_tools" / "mcp_server.py").write_text("# changed\n", encoding="utf-8")
    monkeypatch.setattr(
        mcp_server,
        "_verify_precommit_success",
        lambda root_path: {"ok": True, "message": "ok"},
    )

    result = mcp_server.workflow_status(
        "implementation",
        module="agent_tools",
        instructions_read=True,
        root=str(root),
    )

    assert result["ok"] is False
    assert result["result"]["missing_mandatory_actions"] == [
        "Run version_bump(...) so non-documentation changes include required version/changelog paths: docs/CHANGELOG.md."
    ]


def test_workflow_status_ignores_sensitive_local_state_for_version_bump(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    _init_git_repo(root)
    (root / ".env.local").write_text("SECRET=1\n", encoding="utf-8")
    config = root / "config"
    config.mkdir()
    (config / ".env.production").write_text("SECRET=2\n", encoding="utf-8")
    monkeypatch.setattr(
        mcp_server,
        "_verify_precommit_success",
        lambda root_path: {"ok": True, "message": "ok"},
    )

    result = mcp_server.workflow_status(
        "implementation",
        module="agent_tools",
        instructions_read=True,
        root=str(root),
    )

    assert result["ok"] is True
    assert result["result"]["missing_mandatory_actions"] == []


def test_git_workflow_commit_requires_changelog_for_implementation_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    (root / "agent_tools").mkdir()
    (root / "agent_tools" / "mcp_server.py").write_text("# initial\n", encoding="utf-8")
    _init_git_repo(root)
    (root / "agent_tools" / "mcp_server.py").write_text("# changed\n", encoding="utf-8")
    monkeypatch.setattr(
        mcp_server,
        "_verify_precommit_success",
        lambda root_path: {"ok": True, "message": "ok"},
    )

    result = mcp_server.git_workflow(
        "commit",
        message="Update workflow",
        paths=["agent_tools/mcp_server.py"],
        root=str(root),
    )

    assert result["ok"] is False
    assert result["blockers"][0]["phase"] == "version_bump"
    assert result["result"]["version_bump_requirement"]["missing"] == [
        "docs/CHANGELOG.md",
    ]


def test_git_workflow_commit_requires_version_paths_at_unreleased_threshold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    _write_unreleased_changelog(root, [f"Existing change {index}" for index in range(1, 10)])
    (root / "agent_tools").mkdir()
    (root / "agent_tools" / "mcp_server.py").write_text("# initial\n", encoding="utf-8")
    _init_git_repo(root)
    (root / "agent_tools" / "mcp_server.py").write_text("# changed\n", encoding="utf-8")
    changelog = root / "docs" / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text(encoding="utf-8").replace(
            "- Existing change 9.",
            "- Existing change 9.\n- Tenth change.",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        mcp_server,
        "_verify_precommit_success",
        lambda root_path: {"ok": True, "message": "ok"},
    )

    result = mcp_server.git_workflow(
        "commit",
        message="Update workflow",
        paths=["agent_tools/mcp_server.py", "docs/CHANGELOG.md"],
        root=str(root),
    )

    assert result["ok"] is False
    assert result["blockers"][0]["phase"] == "version_bump"
    assert result["result"]["version_bump_requirement"]["missing"] == [
        "README.md",
        "pyproject.toml",
    ]


def test_git_workflow_commit_allows_unreleased_changelog_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    (root / "agent_tools").mkdir()
    (root / "agent_tools" / "mcp_server.py").write_text("# initial\n", encoding="utf-8")
    _init_git_repo(root)
    (root / "agent_tools" / "mcp_server.py").write_text("# changed\n", encoding="utf-8")
    changelog = root / "docs" / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text(encoding="utf-8").replace(
            "# Changelog\n\n",
            "# Changelog\n\n## Unreleased\n\n- Updated workflow.\n\n",
        ),
        encoding="utf-8",
    )
    commands: list[str] = []
    monkeypatch.setattr(
        mcp_server,
        "_verify_precommit_success",
        lambda root_path: {"ok": True, "message": "ok"},
    )
    monkeypatch.setattr(
        mcp_server,
        "_push_readiness",
        lambda root_path: {"blockers": [], "command_results": [], "repo_health": {"branch": "dev"}},
    )
    monkeypatch.setattr(
        mcp_server,
        "_watch_pushed_commit",
        lambda root_path, sha, timeout_seconds, wait_seconds, detail="summary": {
            "result": {"sha": "a" * 40},
            "command_results": [],
            "blockers": [],
        },
    )
    real_run_command = mcp_server._run_command

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        if command["display"] == "git status --short":
            return real_run_command(root_path, command)
        commands.append(str(command["display"]))
        return {
            "ok": True,
            "command": command["display"],
            "returncode": 0,
            "stdout": "a" * 40 if command["display"] == "git rev-parse HEAD" else "ok",
            "stderr": "",
            "summary": "ok",
        }

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)

    result = mcp_server.git_workflow(
        "commit",
        message="Update workflow",
        paths=["agent_tools/mcp_server.py", "docs/CHANGELOG.md"],
        root=str(root),
    )

    assert result["ok"] is True, result
    assert commands[-4:] == [
        "git add -- agent_tools/mcp_server.py docs/CHANGELOG.md",
        "git commit -m 'Update workflow'",
        "git rev-parse HEAD",
        "git push origin HEAD:dev",
    ]


def test_git_workflow_commit_allows_documentation_only_without_version_bump(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    (root / "docs" / "guide.md").write_text("Initial guide.\n", encoding="utf-8")
    _init_git_repo(root)
    (root / "docs" / "guide.md").write_text("Changed guide.\n", encoding="utf-8")
    commands: list[str] = []
    monkeypatch.setattr(
        mcp_server,
        "_verify_precommit_success",
        lambda root_path: {"ok": True, "message": "ok"},
    )
    monkeypatch.setattr(
        mcp_server,
        "_push_readiness",
        lambda root_path: {"blockers": [], "command_results": [], "repo_health": {"branch": "dev"}},
    )
    monkeypatch.setattr(
        mcp_server,
        "_watch_pushed_commit",
        lambda root_path, sha, timeout_seconds, wait_seconds, detail="summary": {
            "result": {"sha": "a" * 40},
            "command_results": [],
            "blockers": [],
        },
    )

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        commands.append(str(command["display"]))
        return {
            "ok": True,
            "command": command["display"],
            "returncode": 0,
            "stdout": "a" * 40 if command["display"] == "git rev-parse HEAD" else "ok",
            "stderr": "",
            "summary": "ok",
        }

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)

    result = mcp_server.git_workflow(
        "commit",
        message="Update docs",
        paths=["docs/guide.md"],
        root=str(root),
    )

    assert result["ok"] is True
    assert commands[-4:] == [
        "git add -- docs/guide.md",
        "git commit -m 'Update docs'",
        "git rev-parse HEAD",
        "git push origin HEAD:dev",
    ]


def test_git_workflow_commit_allows_agent_tools_readme_without_version_bump(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    agent_tools = root / "agent_tools"
    agent_tools.mkdir()
    (agent_tools / "README.md").write_text("# Agent Tools\n", encoding="utf-8")
    _init_git_repo(root)
    (agent_tools / "README.md").write_text("# Agent Tools\n\nUpdated docs.\n", encoding="utf-8")
    commands: list[str] = []
    monkeypatch.setattr(
        mcp_server,
        "_verify_precommit_success",
        lambda root_path: {"ok": True, "message": "ok"},
    )
    monkeypatch.setattr(
        mcp_server,
        "_push_readiness",
        lambda root_path: {"blockers": [], "command_results": [], "repo_health": {"branch": "dev"}},
    )
    monkeypatch.setattr(
        mcp_server,
        "_watch_pushed_commit",
        lambda root_path, sha, timeout_seconds, wait_seconds, detail="summary": {
            "result": {"sha": "a" * 40},
            "command_results": [],
            "blockers": [],
        },
    )

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        commands.append(str(command["display"]))
        return {
            "ok": True,
            "command": command["display"],
            "returncode": 0,
            "stdout": "a" * 40 if command["display"] == "git rev-parse HEAD" else "ok",
            "stderr": "",
            "summary": "ok",
        }

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)

    result = mcp_server.git_workflow(
        "commit",
        message="Update agent tools docs",
        paths=["agent_tools/README.md"],
        root=str(root),
    )

    assert result["ok"] is True
    assert commands[-4:] == [
        "git add -- agent_tools/README.md",
        "git commit -m 'Update agent tools docs'",
        "git rev-parse HEAD",
        "git push origin HEAD:dev",
    ]


def test_dependency_metadata_status_detects_readme_constraint_mismatch(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    readme = (root / "README.md").read_text(encoding="utf-8")
    (root / "README.md").write_text(
        readme.replace(
            "[requests](https://pypi.org/project/requests/) (`>=2.28.2,<3`)",
            "[requests](https://pypi.org/project/requests/) (`>=2.28.1,<3`)",
        ),
        encoding="utf-8",
    )

    result = mcp_server.dependency_metadata_status(root=str(root))

    assert result["ok"] is False
    assert "README Imports" in result["blockers"][0]["message"]
    assert "do not match pyproject" in result["blockers"][0]["message"]


def test_dependency_metadata_status_detects_malformed_optional_extra(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    readme = (root / "README.md").read_text(encoding="utf-8")
    (root / "README.md").write_text(
        readme.replace("; optional extra `airflow`", ""),
        encoding="utf-8",
    )

    result = mcp_server.dependency_metadata_status(root=str(root))

    assert result["ok"] is False
    assert "Unsupported README Suggests entry" in result["blockers"][0]["message"]


def test_precommit_fingerprint_includes_untracked_file_contents(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    _init_git_repo(root)
    untracked = root / "new_agent_note.txt"
    untracked.write_text("first version\n", encoding="utf-8")

    first = mcp_server._working_tree_fingerprint(root)
    untracked.write_text("second version\n", encoding="utf-8")
    second = mcp_server._working_tree_fingerprint(root)

    assert first != second


def test_precommit_fingerprint_excludes_sensitive_untracked_file_contents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    (root / ".connections").write_text("secret connection\n", encoding="utf-8")
    connections_dir = root / "project_connections" / ".connections"
    connections_dir.mkdir(parents=True)
    (connections_dir / "dev.toml").write_text("nested secret connection\n", encoding="utf-8")
    (root / ".env").mkdir()
    (root / ".env" / "local").write_text("SECRET=1\n", encoding="utf-8")
    (root / ".env.local").write_text("SECRET=2\n", encoding="utf-8")
    config_dir = root / "config"
    config_dir.mkdir()
    (config_dir / ".env.production").write_text("SECRET=3\n", encoding="utf-8")
    certs = root / ".certs"
    certs.mkdir()
    (certs / "client.key").write_text("secret key\n", encoding="utf-8")
    (root / "new_agent_note.txt").write_text("safe note\n", encoding="utf-8")
    read_paths: list[str] = []
    original_read_bytes = Path.read_bytes

    def read_bytes_with_secret_guard(path: Path) -> bytes:
        rel_path = path.relative_to(root).as_posix()
        if mcp_server._is_sensitive_local_path(rel_path):
            msg = f"sensitive path was read: {rel_path}"
            raise AssertionError(msg)
        read_paths.append(rel_path)
        return original_read_bytes(path)

    monkeypatch.setattr(
        mcp_server,
        "_run_git",
        lambda root_path, args: {
            "ok": True,
            "stdout": (
                ".connections\n"
                "project_connections/.connections/dev.toml\n"
                ".env/local\n"
                ".env.local\n"
                "config/.env.production\n"
                ".certs/client.key\n"
                "new_agent_note.txt\n"
            ),
            "stderr": "",
            "returncode": 0,
            "command": "git ls-files --others --exclude-standard",
            "summary": "",
        },
    )
    monkeypatch.setattr(Path, "read_bytes", read_bytes_with_secret_guard)

    parts = mcp_server._untracked_file_fingerprint_parts(root)

    assert read_paths == ["new_agent_note.txt"]
    assert "new_agent_note.txt" in "\n".join(parts)
    assert ".connections:excluded-sensitive-local-path" in parts
    assert "project_connections/.connections/dev.toml:excluded-sensitive-local-path" in parts
    assert ".env/local:excluded-sensitive-local-path" in parts
    assert ".env.local:excluded-sensitive-local-path" in parts
    assert "config/.env.production:excluded-sensitive-local-path" in parts
    assert ".certs/client.key:excluded-sensitive-local-path" in parts


def test_working_tree_fingerprint_excludes_sensitive_tracked_diffs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    commands: list[list[str]] = []

    def fake_run_git(root_path: Path, args: list[str]) -> dict[str, object]:
        commands.append(args)
        if args == ["diff", "--name-only", "-z"]:
            stdout = ".connections/dev.toml\0.env.local\0safe.txt\0"
        elif args == ["diff", "--cached", "--raw", "-z"]:
            stdout = ":100644 100644 old new M\0config/.env.production\0"
        elif args == ["ls-files", "--others", "--exclude-standard"]:
            stdout = ""
        else:
            stdout = "ok\n"
        return {
            "ok": True,
            "stdout": stdout,
            "stderr": "",
            "returncode": 0,
            "command": "git " + " ".join(args),
            "summary": "",
        }

    monkeypatch.setattr(mcp_server, "_run_git", fake_run_git)

    mcp_server._working_tree_fingerprint(root)

    assert ["diff", "--name-only", "-z"] in commands
    assert ["diff", "--cached", "--raw", "-z"] in commands
    assert not any("--binary" in command for command in commands)


def test_check_verification_fails_closed_when_fingerprint_git_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    mcp_server._record_precommit_success(
        root,
        "recorded",
        [{"command": "precommit", "returncode": 0, "summary": "ok"}],
    )
    mcp_server._record_release_check_success(
        root,
        "recorded",
        [{"command": "release check", "returncode": 0, "summary": "ok"}],
    )

    def fake_run_git(root_path: Path, args: list[str]) -> dict[str, object]:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "fatal: not a git repository",
            "returncode": 128,
            "command": "git " + " ".join(args),
            "summary": "fatal: not a git repository",
        }

    monkeypatch.setattr(mcp_server, "_run_git", fake_run_git)

    precommit = mcp_server._verify_precommit_success(root)
    release = mcp_server._verify_release_check_success(root)

    assert precommit["ok"] is False
    assert release["ok"] is False
    assert "Could not fingerprint working tree" in precommit["message"]
    assert precommit["fingerprint_error"]["returncode"] == 128
    assert "Could not fingerprint working tree" in release["message"]
    assert release["fingerprint_error"]["command"] == "git rev-parse HEAD"


def test_git_workflow_blocks_when_untracked_content_changes_after_checks(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    _init_git_repo(root)
    untracked = root / "new_agent_note.txt"
    untracked.write_text("checked contents\n", encoding="utf-8")
    _write_changed_version_metadata(root, "1.3.9.14")
    fingerprint = mcp_server._working_tree_fingerprint(root)
    mcp_server._record_precommit_success(
        root,
        fingerprint,
        [{"command": "precommit", "returncode": 0, "summary": "ok"}],
    )

    untracked.write_text("changed after checks\n", encoding="utf-8")
    result = mcp_server.git_workflow(
        "commit",
        message="Update workflow",
        paths=[
            "new_agent_note.txt",
            "pyproject.toml",
            "README.md",
            "docs/CHANGELOG.md",
        ],
        root=str(root),
    )

    assert result["ok"] is False
    assert result["blockers"][0]["phase"] == "precommit"


def test_git_workflow_commit_and_push_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    commands: list[str] = []
    monkeypatch.setattr(
        mcp_server,
        "_verify_precommit_success",
        lambda root_path: {"ok": True, "message": "ok"},
    )
    monkeypatch.setattr(
        mcp_server,
        "_version_bump_requirement",
        lambda root_path, paths=None: {"missing": []},
    )
    monkeypatch.setattr(
        mcp_server,
        "_push_readiness",
        lambda root_path: {"blockers": [], "command_results": [], "repo_health": {"branch": "dev"}},
    )
    monkeypatch.setattr(
        mcp_server,
        "_run_git",
        lambda root_path, args: {
            "ok": True,
            "stdout": "a" * 40 + "\n",
            "stderr": "",
            "returncode": 0,
            "command": "git " + " ".join(args),
            "summary": "ok",
        },
    )
    monkeypatch.setattr(
        mcp_server,
        "_watch_pushed_commit",
        lambda root_path, sha, timeout_seconds, wait_seconds, detail="summary": {
            "result": {"sha": "a" * 40, "required": []},
            "command_results": [],
            "blockers": [],
        },
    )

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        commands.append(str(command["display"]))
        return {
            "ok": True,
            "command": command["display"],
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "summary": "ok",
        }

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)

    result = mcp_server.git_workflow(
        "commit",
        message="Update workflow",
        paths=["agent_tools/mcp_server.py", "tests/test_agent_tools_mcp.py"],
        root=str(root),
    )

    assert result["ok"] is True
    assert result["summary"] == "Commit, push, and exact-SHA GitHub verification completed."
    assert commands == [
        "git add -- agent_tools/mcp_server.py tests/test_agent_tools_mcp.py",
        "git commit -m 'Update workflow'",
        "git push origin HEAD:dev",
    ]


def test_git_workflow_push_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    commands: list[str] = []
    monkeypatch.setattr(
        mcp_server,
        "_push_readiness",
        lambda root_path: {"blockers": [], "command_results": [], "repo_health": {"branch": "dev"}},
    )
    monkeypatch.setattr(
        mcp_server,
        "_run_git",
        lambda root_path, args: {
            "ok": True,
            "stdout": "a" * 40 + "\n",
            "stderr": "",
            "returncode": 0,
            "command": "git " + " ".join(args),
            "summary": "ok",
        },
    )
    monkeypatch.setattr(
        mcp_server,
        "_watch_pushed_commit",
        lambda root_path, sha, timeout_seconds, wait_seconds, detail="summary": {
            "result": {"sha": "a" * 40, "required": []},
            "command_results": [],
            "blockers": [],
        },
    )

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        commands.append(str(command["display"]))
        return {
            "ok": True,
            "command": command["display"],
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "summary": "ok",
        }

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)

    result = mcp_server.git_workflow("push", root=str(root))

    assert result["ok"] is True
    assert commands == ["git push origin HEAD:dev"]


def test_git_workflow_checks_requires_sha(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")

    result = mcp_server.git_workflow("checks", root=str(root))

    assert result["ok"] is False
    assert result["blockers"][0]["phase"] == "validate"


def test_classify_github_snapshot_requires_workflows_jobs_and_statuses() -> None:
    expected = [
        {
            "name": "sql-integration",
            "required_jobs": ["core SQL integration"],
            "allowed_conclusions": ["success"],
        }
    ]
    snapshot = {
        "runs": [
            {
                "name": "sql-integration",
                "id": 42,
                "status": "completed",
                "conclusion": "success",
                "html_url": "https://example.test/run/42",
            }
        ],
        "jobs": [
            {
                "workflow_run_id": 42,
                "name": "core SQL integration",
                "status": "completed",
                "conclusion": "success",
                "html_url": "https://example.test/job/7",
            }
        ],
        "check_runs": [],
        "statuses": [],
    }

    result = mcp_server._classify_github_snapshot(expected, snapshot)

    assert result["missing"] == []
    assert result["pending"] == []
    assert result["failed"] == []
    assert result["required"][0]["run_id"] == 42


def test_github_watcher_handles_delayed_discovery_and_exact_sha(
    tmp_path: Path,
) -> None:
    root = _write_watcher_manifest(tmp_path / "project")
    sha = "a" * 40
    clock = _FakeClock()
    snapshots = [
        {"runs": [], "jobs": [], "check_runs": [], "statuses": []},
        _successful_github_snapshot(),
    ]
    runner = _FakeGithubRunner(sha, snapshots)

    result = mcp_server._watch_github_checks(
        root,
        sha=sha,
        timeout_seconds=30,
        poll_seconds=1,
        discovery_seconds=10,
        command_runner=runner,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        detail="diagnostic",
    )

    assert result["blockers"] == []
    assert result["result"]["sha"] == sha
    assert result["result"]["required"][0]["run_id"] == 42
    assert all(f"head_sha={sha}" in endpoint for endpoint in runner.run_endpoints)
    assert result["result"]["total_duration_seconds"] == 1


def test_github_watcher_returns_resumable_pending_slice_and_status_changes(
    tmp_path: Path,
) -> None:
    root = _write_watcher_manifest(tmp_path / "project")
    sha = "f" * 40
    pending_snapshot: Any = _successful_github_snapshot()
    pending_snapshot["runs"][0]["status"] = "in_progress"
    pending_snapshot["runs"][0]["conclusion"] = None
    clock = _FakeClock()

    pending = mcp_server._watch_github_checks(
        root,
        sha=sha,
        timeout_seconds=30,
        poll_seconds=1,
        discovery_seconds=10,
        wait_seconds=2,
        command_runner=_FakeGithubRunner(sha, [pending_snapshot]),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert pending["blockers"] == []
    assert pending["result"]["status"] == "pending"
    assert pending["result"]["watch_id"] == sha
    assert pending["result"]["changes"][0]["name"] == "tests"
    assert len(pending["command_results"]) == 1
    assert "required" not in pending["result"]
    assert pending["result"]["pending_required"] == ["tests"]
    pending_payload = mcp_server._tool_output(
        "git_workflow",
        {"detail": "summary", "action": "checks", "sha": sha},
        result={"github_checks": pending["result"]},
        command_results=pending["command_results"],
    )
    assert pending_payload["telemetry"]["response_bytes"] <= 1_500
    assert pending_payload["telemetry"]["within_budget"] is True

    completed = mcp_server._watch_github_checks(
        root,
        sha=sha,
        timeout_seconds=30,
        poll_seconds=1,
        discovery_seconds=10,
        wait_seconds=2,
        command_runner=_FakeGithubRunner(sha, [_successful_github_snapshot()]),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert completed["result"]["status"] == "complete"
    assert completed["result"]["changes"][0]["before"]["status"] == "in_progress"
    assert completed["result"]["changes"][0]["after"]["conclusion"] == "success"
    assert completed["command_results"] == []


def test_first_pending_commit_receipt_stays_within_budget() -> None:
    sha = "a" * 40
    changes = [
        {
            "name": f"check {index}",
            "kind": "check_run",
            "before": None,
            "after": {
                "kind": "check_run",
                "status": "in_progress",
                "url": f"https://example.test/check/{index}",
            },
        }
        for index in range(17)
    ]
    github = mcp_server._github_result_receipt(
        {
            "sha": sha,
            "repository": "owner/repository",
            "push_target": "origin/dev",
            "status": "pending",
            "watch_id": sha,
            "resume_after_seconds": 15,
            "changes": changes,
            "pending": [f"check-run: check {index}" for index in range(15)],
            "missing": [],
            "total_duration_seconds": 60,
        },
        detail="summary",
    )
    payload = mcp_server._tool_output(
        "git_workflow",
        {
            "action": "commit",
            "message": "Update agent workflow",
            "paths": [f"path-{index}.py" for index in range(10)],
            "detail": "summary",
        },
        result={
            "mutation": {
                "sha": sha,
                "path_count": 10,
                "push_target": "origin/dev",
            },
            "github_checks": github,
        },
        next_actions=[f"Resume with git_workflow(action='checks', sha='{sha}')."],
    )

    assert github["change_count"] == 17
    assert len(github["changes"]) == 3
    assert github["pending_required_count"] == 15
    assert len(github["pending_required"]) == 5
    assert all("url" not in change for change in github["changes"])
    assert "paths_digest" not in payload["input"]
    assert "message" not in payload["result"]["mutation"]
    assert payload["telemetry"]["response_bytes"] <= 1_500
    assert payload["telemetry"]["within_budget"] is True


def test_unchanged_pending_receipt_omits_repeated_check_names() -> None:
    receipt = mcp_server._github_result_receipt(
        {
            "sha": "a" * 40,
            "status": "pending",
            "watch_id": "a" * 40,
            "resume_after_seconds": 15,
            "changes": [],
            "pending": ["tests", "sql integration"],
            "missing": [],
        },
        detail="summary",
    )

    assert receipt["pending_required_count"] == 2
    assert "pending_required" not in receipt
    assert "missing" not in receipt


def test_completed_summary_omits_job_level_details() -> None:
    receipt = mcp_server._github_result_receipt(
        {
            "sha": "a" * 40,
            "status": "complete",
            "required": [
                {
                    "name": "tests",
                    "conclusion": "success",
                    "url": "https://example.test/run",
                    "jobs": [{"name": "py3.14", "conclusion": "success"}],
                }
            ],
        },
        detail="summary",
    )

    assert receipt["required"] == [
        {
            "name": "tests",
            "conclusion": "success",
            "url": "https://example.test/run",
        }
    ]


def test_github_snapshot_discards_successful_api_command_payloads(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    snapshot = _successful_github_snapshot()
    runner = _FakeGithubRunner("a" * 40, [snapshot])

    result = mcp_server._github_check_snapshot(
        root,
        "owner/repository",
        "a" * 40,
        command_runner=runner,
    )

    assert result["runs"][0]["id"] == 42
    assert result["command_results"] == []


def test_github_watcher_rejects_missing_required_workflow_after_grace(
    tmp_path: Path,
) -> None:
    root = _write_watcher_manifest(tmp_path / "project")
    clock = _FakeClock()
    runner = _FakeGithubRunner(
        "b" * 40, [{"runs": [], "jobs": [], "check_runs": [], "statuses": []}]
    )

    result = mcp_server._watch_github_checks(
        root,
        sha="b" * 40,
        timeout_seconds=20,
        poll_seconds=1,
        discovery_seconds=2,
        command_runner=runner,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert result["blockers"][0]["phase"] == "github_checks_discovery"
    assert result["blockers"][0]["missing"] == ["tests"]


def test_github_watcher_accepts_only_declared_conditional_skip(tmp_path: Path) -> None:
    root = _write_watcher_manifest(tmp_path / "project", conditional=True)
    snapshot = _successful_github_snapshot()
    snapshot["check_runs"] = [
        {
            "name": "nightly fault",
            "status": "completed",
            "conclusion": "skipped",
            "html_url": "https://example.test/check/9",
        }
    ]
    result = mcp_server._watch_github_checks(
        root,
        sha="c" * 40,
        command_runner=_FakeGithubRunner("c" * 40, [snapshot]),
        monotonic=lambda: 0.0,
        sleeper=lambda _: None,
    )

    assert result["blockers"] == []
    assert result["result"]["conditional_skips_accepted"] == [
        {"name": "nightly fault", "conclusion": "skipped", "kind": "check_run"}
    ]

    snapshot["check_runs"][0]["name"] = "undeclared"
    rejected = mcp_server._watch_github_checks(
        root,
        sha="c" * 40,
        command_runner=_FakeGithubRunner("c" * 40, [snapshot]),
        monotonic=lambda: 0.0,
        sleeper=lambda _: None,
    )
    assert rejected["blockers"][0]["phase"] == "github_checks"


def test_github_watcher_reports_failed_steps_and_failed_log_error(tmp_path: Path) -> None:
    root = _write_watcher_manifest(tmp_path / "project")
    snapshot = _successful_github_snapshot()
    snapshot["jobs"][0]["conclusion"] = "failure"
    snapshot["jobs"][0]["steps"] = [{"name": "Run tests", "conclusion": "failure"}]
    runner = _FakeGithubRunner("d" * 40, [snapshot], fail_logs=True)

    result = mcp_server._watch_github_checks(
        root,
        sha="d" * 40,
        command_runner=runner,
        monotonic=lambda: 0.0,
        sleeper=lambda _: None,
    )

    assert result["blockers"][0]["phase"] == "github_checks"
    assert result["result"]["failures"][0]["failed_steps"] == [
        {"name": "Run tests", "conclusion": "failure"}
    ]
    assert result["result"]["failed_log_excerpts"][0]["ok"] is False
    assert "log unavailable" in result["result"]["failed_log_excerpts"][0]["excerpt"]


def test_push_captures_immutable_sha_before_push(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    captured = "e" * 40
    order: list[str] = []
    monkeypatch.setattr(
        mcp_server,
        "_push_readiness",
        lambda root_path: {"blockers": [], "command_results": [], "repo_health": {"branch": "dev"}},
    )

    def fake_git(root_path: Path, args: list[str]) -> dict[str, object]:
        order.append("sha")
        return {
            "ok": True,
            "stdout": captured + "\n",
            "stderr": "",
            "returncode": 0,
            "command": "git rev-parse HEAD",
            "summary": captured,
        }

    def fake_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        order.append("push")
        return {
            "ok": True,
            "stdout": "",
            "stderr": "",
            "returncode": 0,
            "command": command["display"],
            "summary": "ok",
        }

    monkeypatch.setattr(mcp_server, "_run_git", fake_git)
    monkeypatch.setattr(mcp_server, "_run_command", fake_command)

    result = mcp_server._push_dev_result(root)

    assert order == ["sha", "push"]
    assert result["sha"] == captured


def test_push_result_does_not_duplicate_nested_command_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    readiness_command = _command_result("git fetch origin dev", "x" * 50_000)
    monkeypatch.setattr(
        mcp_server,
        "_push_readiness",
        lambda root_path: {
            "blockers": [],
            "command_results": [readiness_command],
            "repo_health": {"branch": "dev"},
            "remote_dev_status": {"contains_origin_dev": True},
        },
    )
    monkeypatch.setattr(
        mcp_server,
        "_run_git",
        lambda root_path, args: _command_result("git rev-parse HEAD", "a" * 40 + "\n"),
    )
    monkeypatch.setattr(
        mcp_server,
        "_run_command",
        lambda root_path, command: _command_result(str(command["display"]), "pushed"),
    )

    push = mcp_server._push_dev_result(root)
    payload = mcp_server._tool_output(
        "git_workflow",
        {"detail": "summary"},
        result={"push_readiness": push["readiness"]},
        command_results=push["command_results"],
    )

    assert "command_results" not in push["readiness"]
    assert payload["telemetry"]["response_bytes"] < 8_000
    assert len(json.dumps(payload)) < 8_000


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


def test_git_workflow_commit_reports_push_readiness_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    commands: list[str] = []
    monkeypatch.setattr(
        mcp_server,
        "_verify_precommit_success",
        lambda root_path: {"ok": True, "message": "ok"},
    )
    monkeypatch.setattr(
        mcp_server,
        "_version_bump_requirement",
        lambda root_path, paths=None: {"missing": []},
    )
    monkeypatch.setattr(
        mcp_server,
        "_push_readiness",
        lambda root_path: {
            "blockers": [{"phase": "push", "message": "Push workflow must run from dev."}],
            "command_results": [],
            "repo_health": {"branch": "feature"},
        },
    )

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        commands.append(str(command["display"]))
        return {
            "ok": True,
            "command": command["display"],
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "summary": "ok",
        }

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)

    result = mcp_server.git_workflow(
        "commit",
        message="Update workflow",
        paths=["agent_tools/mcp_server.py"],
        root=str(root),
    )

    assert result["ok"] is False
    assert result["summary"] == "Commit completed, but push to dev failed."
    assert result["blockers"][0]["phase"] == "push"
    assert commands == [
        "git add -- agent_tools/mcp_server.py",
        "git commit -m 'Update workflow'",
    ]


def test_git_workflow_commit_reports_push_command_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    commands: list[str] = []
    monkeypatch.setattr(
        mcp_server,
        "_verify_precommit_success",
        lambda root_path: {"ok": True, "message": "ok"},
    )
    monkeypatch.setattr(
        mcp_server,
        "_version_bump_requirement",
        lambda root_path, paths=None: {"missing": []},
    )
    monkeypatch.setattr(
        mcp_server,
        "_push_readiness",
        lambda root_path: {"blockers": [], "command_results": [], "repo_health": {"branch": "dev"}},
    )
    monkeypatch.setattr(mcp_server, "_run_git", _successful_head)

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        display = str(command["display"])
        commands.append(display)
        ok = not display.startswith("git push ")
        return {
            "ok": ok,
            "command": command["display"],
            "returncode": 0 if ok else 1,
            "stdout": "ok" if ok else "",
            "stderr": "" if ok else "network unavailable",
            "summary": "ok" if ok else "network unavailable",
        }

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)

    result = mcp_server.git_workflow(
        "commit",
        message="Update workflow",
        paths=["agent_tools/mcp_server.py"],
        root=str(root),
    )

    assert result["ok"] is False
    assert result["blockers"][0]["phase"] == "push"
    assert commands == [
        "git add -- agent_tools/mcp_server.py",
        "git commit -m 'Update workflow'",
        "git push origin HEAD:dev",
    ]


def test_git_workflow_cli_accepts_explicit_commit_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_git_workflow(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "git_workflow", fake_git_workflow)
    parser = mcp_server._build_cli_parser()
    args = parser.parse_args(
        [
            "git-workflow",
            "commit",
            "--message",
            "Update workflow",
            "--path",
            "agent_tools/mcp_server.py",
            "--path",
            "tests/test_agent_tools_mcp.py",
        ]
    )

    result = args.handler(args)

    assert result == {"ok": True}
    assert captured["action"] == "commit"
    assert captured["message"] == "Update workflow"
    assert captured["paths"] == [
        "agent_tools/mcp_server.py",
        "tests/test_agent_tools_mcp.py",
    ]


def test_git_workflow_cli_rejects_precommit_bypass_flag() -> None:
    parser = mcp_server._build_cli_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            [
                "git-workflow",
                "commit",
                "--message",
                "Update workflow",
                "--path",
                "agent_tools/mcp_server.py",
                "--allow-without-checks",
            ]
        )

    assert exc_info.value.code == 2


def test_git_workflow_push_blocks_on_readiness_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    monkeypatch.setattr(
        mcp_server,
        "_push_readiness",
        lambda root_path: {
            "blockers": [{"phase": "push", "message": "Push workflow must run from dev."}],
            "command_results": [],
            "repo_health": {"branch": "feature"},
        },
    )

    def fail_if_called(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        msg = "push command should not run"
        raise AssertionError(msg)

    monkeypatch.setattr(mcp_server, "_run_command", fail_if_called)

    result = mcp_server.git_workflow("push", root=str(root))

    assert result["ok"] is False
    assert result["blockers"][0]["message"] == "Push workflow must run from dev."


def test_push_readiness_blocks_when_origin_dev_is_not_ancestor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    monkeypatch.setattr(
        mcp_server,
        "repo_health",
        lambda root: {"branch": "dev", "dirty": False},
    )
    monkeypatch.setattr(
        mcp_server,
        "_remote_dev_status",
        lambda root_path, require_equal: {
            "result": {"contains_origin_dev": False},
            "command_results": [],
            "blockers": [
                {
                    "phase": "remote_dev",
                    "message": "Local HEAD does not contain origin/dev; pull, rebase, or merge before continuing.",
                }
            ],
        },
    )

    result = mcp_server._push_readiness(root)

    assert result["blockers"][0]["phase"] == "remote_dev"


def test_release_workflow_status_reports_readiness_blockers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    monkeypatch.setattr(
        mcp_server,
        "_run_git",
        lambda root_path, args: {
            "ok": True,
            "stdout": "feature\n" if args == ["branch", "--show-current"] else "",
            "stderr": "",
            "returncode": 0,
            "command": "git",
            "summary": "",
        },
    )

    result = mcp_server.release_workflow("status", root=str(root))

    assert result["ok"] is False
    assert result["blockers"][0]["message"] == "Release publishing must run from main."


def test_release_workflow_publish_delegates_to_release_script(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    commands: list[str] = []
    monkeypatch.setattr(
        mcp_server,
        "_release_readiness",
        lambda root_path, require_release_check=False: {
            "blockers": [],
            "repo_health": {"branch": "main"},
            "command_results": [],
        },
    )

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        commands.append(str(command["display"]))
        return {
            "ok": True,
            "command": command["display"],
            "returncode": 0,
            "stdout": "published",
            "stderr": "",
            "summary": "published",
        }

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)

    result = mcp_server.release_workflow("publish", root=str(root))

    assert result["ok"] is True
    assert commands == ["release_routines/pypi_release.sh"]


def test_release_workflow_merge_dev_fast_forwards_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    commands: list[str] = []
    monkeypatch.setattr(
        mcp_server,
        "repo_health",
        lambda root: {"branch": "dev", "dirty": False},
    )

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        commands.append(str(command["display"]))
        return {
            "ok": True,
            "command": command["display"],
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "summary": "ok",
        }

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)

    result = mcp_server.release_workflow("merge-dev", root=str(root))

    assert result["ok"] is True
    assert commands == [
        "git fetch origin main",
        "git fetch origin dev",
        "git switch main",
        "git pull --ff-only origin main",
        "git merge --ff-only origin/dev",
        "git push origin main",
    ]


def test_release_readiness_blocks_when_main_lacks_origin_dev(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    monkeypatch.setattr(
        mcp_server,
        "repo_health",
        lambda root: {"branch": "main", "dirty": False},
    )
    monkeypatch.setattr(
        mcp_server,
        "_remote_main_status",
        lambda root_path, require_equal: {
            "result": {"matches_origin_main": True},
            "command_results": [],
            "blockers": [],
        },
    )
    monkeypatch.setattr(
        mcp_server,
        "_remote_dev_status",
        lambda root_path, require_equal: {
            "result": {"contains_origin_dev": False},
            "command_results": [],
            "blockers": [
                {
                    "phase": "remote_dev",
                    "message": "Local HEAD does not contain origin/dev; pull, rebase, or merge before continuing.",
                }
            ],
        },
    )

    result = mcp_server.release_workflow("status", root=str(root))

    assert result["ok"] is False
    assert result["blockers"][0]["phase"] == "remote_dev"


def test_release_status_blocks_without_current_precommit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    monkeypatch.setattr(
        mcp_server,
        "repo_health",
        lambda root: {"branch": "main", "dirty": False},
    )
    monkeypatch.setattr(
        mcp_server,
        "_remote_main_status",
        lambda root_path, require_equal: {
            "result": {"matches_origin_main": True},
            "command_results": [],
            "blockers": [],
        },
    )
    monkeypatch.setattr(
        mcp_server,
        "_remote_dev_status",
        lambda root_path, require_equal: {
            "result": {"contains_origin_dev": True},
            "command_results": [],
            "blockers": [],
        },
    )

    result = mcp_server.release_workflow("status", root=str(root))

    assert result["ok"] is False
    assert result["blockers"][0]["phase"] == "precommit"


def test_release_status_records_release_check_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    commands: list[str] = []
    monkeypatch.setattr(
        mcp_server,
        "_release_readiness",
        lambda root_path, require_release_check=False: {
            "blockers": [],
            "repo_health": {"branch": "main"},
            "command_results": [],
        },
    )
    monkeypatch.setattr(mcp_server, "_working_tree_fingerprint", lambda root_path: "tree")

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        commands.append(str(command["display"]))
        return {
            "ok": True,
            "command": command["display"],
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "summary": "ok",
        }

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)

    result = mcp_server.release_workflow("status", root=str(root))

    assert result["ok"] is True
    assert result["result"]["release_check_verification"]["ok"] is True
    assert commands == [
        "release_routines/scripts/check_package_metadata.sh",
        "release_routines/scripts/check_readme_dependencies.sh",
        "release_routines/scripts/check_docs_links.sh",
        "release_routines/scripts/check_docs_coverage.sh",
    ]


def test_release_publish_requires_current_release_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    seen_require_release_check: list[bool] = []

    def fake_readiness(root_path: Path, require_release_check: bool = False) -> dict[str, object]:
        seen_require_release_check.append(require_release_check)
        return {
            "blockers": [
                {"phase": "release_checks", "message": "No successful release check record exists."}
            ],
            "command_results": [],
        }

    monkeypatch.setattr(mcp_server, "_release_readiness", fake_readiness)

    result = mcp_server.release_workflow("publish", root=str(root))

    assert result["ok"] is False
    assert seen_require_release_check == [True]
    assert result["blockers"][0]["phase"] == "release_checks"


def test_mcp_tool_wrapper_uses_consolidated_cli_names() -> None:
    script = mcp_server.REPO_ROOT / "agent_tools" / "mcp_tool.sh"

    completed = subprocess.run(
        [str(script), "version-bump", "Wrapper dry run", "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )

    output = json.loads(completed.stdout)
    assert output["ok"] is True
    assert output["tool"] == "version_bump"
    changelog = (mcp_server.REPO_ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    expected_decision = (
        "bump"
        if mcp_server._count_unreleased_changelog_bullets(changelog) + 1
        >= mcp_server.UNRELEASED_CHANGELOG_THRESHOLD
        else "unreleased"
    )
    assert output["result"]["decision"] == expected_decision


def test_mcp_tool_wrapper_routes_force_release_flag(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    _write_unreleased_changelog(root, ["Existing change"])
    script = mcp_server.REPO_ROOT / "agent_tools" / "mcp_tool.sh"

    completed = subprocess.run(
        [
            str(script),
            "version-bump",
            "--change-type",
            "release",
            "--force-release",
            "--dry-run",
            "--root",
            str(root),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    output = json.loads(completed.stdout)
    assert output["ok"] is True
    assert output["input"]["force_release"] is True
    assert output["result"]["decision"] == "bump"


def test_agent_docs_cleanup_removed_direct_docs_assistant_workflow() -> None:
    agents = (mcp_server.REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    development = (mcp_server.REPO_ROOT / "agent_docs" / "development.md").read_text(
        encoding="utf-8"
    )
    agent_tools_readme = (mcp_server.REPO_ROOT / "agent_tools" / "README.md").read_text(
        encoding="utf-8"
    )

    assert "search-docs" not in agents
    assert "ask-docs" not in agents
    assert "route-context" not in agents
    assert "repo-health" not in agents
    assert "python agent_tools/docs_assistant.py" not in agents
    assert "tests/test_sql_connection_config.py tests/test_sql_retries.py" not in development
    assert "Use `docs_assistant.py` for local documentation retrieval" not in agent_tools_readme


def test_precommit_emits_stages_and_seeds_python38_compatible_pip() -> None:
    script = (mcp_server.REPO_ROOT / "release_routines" / "pre_commit_checks.sh").read_text(
        encoding="utf-8"
    )

    assert "::agent-check-stage::%s::start::running" in script
    assert "::agent-check-stage::%s::end::failed" in script
    assert "export VIRTUALENV_PIP=25.0.1" in script
    assert 'export PIP_CACHE_DIR="${PIP_CACHE_DIR:-${repo_root}/.tox/pip-cache}"' in script
    assert 'mode="${1:---all}"' in script
    assert "run_stage tox-static tox -e lint,type" in script
    assert "run_stage tox-coverage tox -e coverage" in script
    assert "run_stage tox-artifacts tox -e artifacts" in script
    assert 'local parallelism="${PRECOMMIT_PARALLELISM:-3}"' in script
    assert "tox run-parallel" in script
    assert "py311-latest" not in next(
        line for line in script.splitlines() if "run_stage tox-matrix" in line
    )


def test_precommit_check_plan_separates_ordered_stages() -> None:
    commands = mcp_server._check_commands(
        area="agent_tools",
        change_type="implementation",
        level="precommit",
    )

    assert [command["display"] for command in commands] == [
        "release_routines/pre_commit_checks.sh --static",
        "release_routines/pre_commit_checks.sh --coverage",
        "release_routines/pre_commit_checks.sh --artifacts",
        "release_routines/pre_commit_checks.sh --matrix",
    ]


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
