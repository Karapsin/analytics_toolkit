from __future__ import annotations

from tests.agent_tools._support.mcp import (
    Path,
    _command_result,
    _write_minimal_repo_files,
    json,
    mcp_server,
    pytest,
    sys,
)


def test_command_blocker_ignores_stage_only_stderr_for_actionable_excerpt() -> None:
    result = _command_result(
        "validation",
        "FAILED tests/test_example.py::test_case - assertion",
        ok=False,
        stderr="::agent-check-stage::pytest::end::failed",
    )

    blocker = mcp_server._command_blocker("run_checks", result)

    assert blocker["excerpt"].startswith("FAILED tests/test_example.py::test_case")


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
