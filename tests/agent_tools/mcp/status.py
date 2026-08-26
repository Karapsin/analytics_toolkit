from __future__ import annotations

from tests.agent_tools._support.mcp import (
    Path,
    _init_git_repo,
    _write_minimal_repo_files,
    json,
    mcp_server,
    pytest,
)


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
            ("diff", "--cached", "--stat"): " tests/agent_tools/mcp/git.py | 3 +++\n",
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
        " tests/agent_tools/mcp/git.py | 3 +++"
    ]
    assert "agent_docs/development.md" in result["result"]["required_instruction_files"]
    assert "agent_docs/release.md" in result["result"]["required_instruction_files"]
    assert "agent_tools/README.md" in result["result"]["required_instruction_files"]
    assert result["result"]["metadata_status"]["ok"] is True
    assert result["result"]["recommended_checks"]["focused_commands"] == [
        "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/agent_tools tests/repository"
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
