from __future__ import annotations

from tests.agent_tools._support.mcp import (
    Path,
    _init_git_repo,
    _successful_head,
    _write_changed_version_metadata,
    _write_minimal_repo_files,
    mcp_server,
    pytest,
)


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


def test_git_workflow_checks_requires_sha(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")

    result = mcp_server.git_workflow("checks", root=str(root))

    assert result["ok"] is False
    assert result["blockers"][0]["phase"] == "validate"


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
            "tests/agent_tools/mcp/git.py",
        ]
    )

    result = args.handler(args)

    assert result == {"ok": True}
    assert captured["action"] == "commit"
    assert captured["message"] == "Update workflow"
    assert captured["paths"] == [
        "agent_tools/mcp_server.py",
        "tests/agent_tools/mcp/git.py",
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
        paths=["agent_tools/mcp_server.py", "tests/agent_tools/mcp/git.py"],
        root=str(root),
    )

    assert result["ok"] is True
    assert result["summary"] == "Commit, push, and exact-SHA GitHub verification completed."
    assert commands == [
        "git add -- agent_tools/mcp_server.py tests/agent_tools/mcp/git.py",
        "git commit -m 'Update workflow'",
        "git push origin HEAD:dev",
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
