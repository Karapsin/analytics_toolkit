from __future__ import annotations

from tests.agent_tools._support.mcp import (
    Path,
    _command_result,
    _write_minimal_repo_files,
    mcp_server,
    pytest,
    types,
)


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
            ("diff", "--cached", "--stat"): " tests/agent_tools/mcp/git.py | 3 +++\n",
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
    assert result["staged_diff_stat"] == [" tests/agent_tools/mcp/git.py | 3 +++"]


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
