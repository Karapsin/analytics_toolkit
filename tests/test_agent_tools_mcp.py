from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

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


def test_prepare_start_stops_on_failed_step(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    assert result["blockers"][0]["stderr"] == "network unavailable"
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

    assert result["ok"] is True
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
    assert search_result["result"]["results"][0]["path"] == "agent_tools/README.md"
    assert ask_result["result"]["answer"].startswith("Most relevant passages")
    assert ask_result["result"]["citations"][0].startswith("agent_tools/README.md:L")


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
    assert result["input"]["index_dir"] == ".rag_index"
    assert result["input"]["resolved_index_dir"] == str(root / ".rag_index")
    assert result["result"]["results"][0]["path"] == "agent_tools/README.md"


def test_workflow_status_combines_routing_health_metadata_and_checks(
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
        "implementation release workflow",
        module="agent_tools",
        instructions_read=True,
        root=str(root),
    )

    assert result["ok"] is True
    assert result["tool"] == "workflow_status"
    assert result["result"]["repo_health"]["branch"] == "main"
    assert "agent_docs/development.md" in result["result"]["required_instruction_files"]
    assert "agent_docs/release.md" in result["result"]["required_instruction_files"]
    assert "agent_tools/README.md" in result["result"]["required_instruction_files"]
    assert result["result"]["metadata_status"]["ok"] is True
    assert result["result"]["recommended_checks"]["focused_commands"] == [
        "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_rag_docs.py tests/test_agent_tools_mcp.py"
    ]


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

    dry_run = mcp_server.version_bump("Consolidated agent MCP workflow", root=str(root), dry_run=True)
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

    dry_run = mcp_server.version_bump("Tenth change", root=str(root), dry_run=True)
    applied = mcp_server.version_bump("Tenth change", root=str(root))

    assert dry_run["result"]["decision"] == "bump"
    assert dry_run["result"]["planned_version"] == "1.3.9.14"
    assert 'version = "1.3.9.14"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "**Version:** `1.3.9.14`" in (root / "README.md").read_text(encoding="utf-8")
    changelog = (root / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## Unreleased" not in changelog
    assert "## 1.3.9.14 - " in changelog
    assert "- Existing change 1." in changelog
    assert "- Tenth change." in changelog
    assert applied["ok"] is True


def test_version_bump_fails_when_readme_version_marker_is_missing_at_threshold(tmp_path: Path) -> None:
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
        raise ValueError("Could not update changelog")

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

    dry_run = mcp_server.run_checks(area="agent_tools", level="focused", root=str(root), dry_run=True)
    assert dry_run["result"]["planned_commands"] == [
        "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_rag_docs.py tests/test_agent_tools_mcp.py"
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
    real_run_command = mcp_server._run_command

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        if command["display"] == "git status --short":
            return real_run_command(root_path, command)
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
        paths=["agent_tools/mcp_server.py", "docs/CHANGELOG.md"],
        root=str(root),
    )

    assert result["ok"] is True
    assert commands[-3:] == [
        "git add -- agent_tools/mcp_server.py docs/CHANGELOG.md",
        "git commit -m 'Update workflow'",
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
        message="Update docs",
        paths=["docs/guide.md"],
        root=str(root),
    )

    assert result["ok"] is True
    assert commands[-3:] == [
        "git add -- docs/guide.md",
        "git commit -m 'Update docs'",
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
        message="Update agent tools docs",
        paths=["agent_tools/README.md"],
        root=str(root),
    )

    assert result["ok"] is True
    assert commands[-3:] == [
        "git add -- agent_tools/README.md",
        "git commit -m 'Update agent tools docs'",
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
            raise AssertionError(f"sensitive path was read: {rel_path}")
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
        if args == ["diff", "--name-only"]:
            stdout = ".connections/dev.toml\n.env.local\nsafe.txt\n"
        elif args == ["diff", "--cached", "--name-only"]:
            stdout = "config/.env.production\nstaged_safe.txt\n"
        elif args in (
            ["diff", "--binary", "--", "safe.txt"],
            ["diff", "--cached", "--binary", "--", "staged_safe.txt"],
        ):
            stdout = "safe diff contents\n"
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

    assert ["diff", "--binary", "--", ".connections/dev.toml"] not in commands
    assert ["diff", "--binary", "--", ".env.local"] not in commands
    assert ["diff", "--cached", "--binary", "--", "config/.env.production"] not in commands
    assert ["diff", "--binary", "--", "safe.txt"] in commands
    assert ["diff", "--cached", "--binary", "--", "staged_safe.txt"] in commands


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
    assert result["summary"] == "Commit completed and pushed to dev."
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
        raise AssertionError("push command should not run")

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
            "blockers": [{"phase": "release_checks", "message": "No successful release check record exists."}],
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
    assert output["result"]["decision"] == "unreleased"


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
        "\n".join(
            [
                "# Agent Tools",
                "",
                "The MCP docs tool provides local RAG retrieval for agents.",
                "Use docs(query, mode=\"search\") for snippets.",
            ]
        ),
        encoding="utf-8",
    )
    return root


def _write_changed_version_metadata(root: Path, version: str) -> None:
    pyproject = root / "pyproject.toml"
    readme = root / "README.md"
    changelog = root / "docs" / "CHANGELOG.md"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace('version = "1.3.9.13"', f'version = "{version}"'),
        encoding="utf-8",
    )
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("**Version:** `1.3.9.13`", f"**Version:** `{version}`"),
        encoding="utf-8",
    )
    changelog.write_text(
        f"# Changelog\n\n## {version} - 2026-06-16\n\n- Updated workflow.\n\n"
        + changelog.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
