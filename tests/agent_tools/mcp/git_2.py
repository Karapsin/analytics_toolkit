from __future__ import annotations

from tests.agent_tools._support.mcp import (
    Path,
    _command_result,
    _init_git_repo,
    _write_minimal_repo_files,
    _write_unreleased_changelog,
    json,
    mcp_server,
    pytest,
)


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
