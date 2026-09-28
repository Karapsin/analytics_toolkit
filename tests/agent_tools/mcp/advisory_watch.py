from __future__ import annotations

from tests.agent_tools._support.mcp import (
    Path,
    _FakeGithubRunner,
    _successful_github_snapshot,
    _write_watcher_manifest,
    json,
    mcp_server,
    pytest,
)


@pytest.mark.parametrize("advisory_state", ["in_progress", "failure", "success"])
@pytest.mark.parametrize("required_state", ["success", "failure"])
def test_watch_never_requests_advisory_jobs_or_diagnostics(
    tmp_path: Path, advisory_state: str, required_state: str
) -> None:
    root = _write_watcher_manifest(tmp_path / "project")
    path = root / ".github" / "required-workflows.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["branches"]["dev"]["workflows"].append(
        {
            "name": "sql-integration",
            "classification": "advisory_push",
            "required_jobs": [{"name": "core integration"}],
        }
    )
    path.write_text(json.dumps(manifest), encoding="utf-8")
    snapshot = _successful_github_snapshot(required_state)
    status = "in_progress" if advisory_state == "in_progress" else "completed"
    conclusion = None if advisory_state == "in_progress" else advisory_state
    snapshot["runs"].append(
        {
            "name": "sql-integration",
            "id": 99,
            "status": status,
            "conclusion": conclusion,
            "html_url": "https://example.test/run/99",
        }
    )
    snapshot["check_runs"].append(
        {
            "name": "core integration",
            "status": status,
            "conclusion": conclusion,
        }
    )
    runner = _FakeGithubRunner("a" * 40, [snapshot])
    commands = []

    def required_only(root_path: Path, command: dict) -> dict:
        display = str(command["display"])
        commands.append(display)
        assert "/runs/99/" not in display
        assert "gh run view 99" not in display
        assert "download" not in display
        assert "artifacts" not in display
        return runner(root_path, command)

    result = mcp_server._watch_github_checks(
        root,
        sha="a" * 40,
        command_runner=required_only,
        monotonic=lambda: 0.0,
        sleeper=lambda _: None,
    )

    assert bool(result["blockers"]) is (required_state == "failure")
    if required_state == "success":
        assert result["result"]["advisory"][0]["status"] == status
        assert "jobs" not in result["result"]["advisory"][0]
    else:
        assert result["blockers"][0]["phase"] == "github_checks"
        assert any("gh run view 42" in command for command in commands)
    assert any("/runs/42/jobs?" in command for command in commands)
