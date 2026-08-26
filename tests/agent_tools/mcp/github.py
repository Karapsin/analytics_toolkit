from __future__ import annotations

from tests.agent_tools._support.mcp import (
    Any,
    Path,
    _FakeClock,
    _FakeGithubRunner,
    _successful_github_snapshot,
    _write_minimal_repo_files,
    _write_watcher_manifest,
    mcp_server,
    pytest,
)


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


def test_classify_github_snapshot_reports_advisory_integration_without_blocking() -> None:
    expected = [{"name": "tests", "allowed_conclusions": ["success"]}]
    advisory = [
        {
            "name": "sql-integration",
            "required_jobs": [{"name": "core SQL integration (HTTP)"}],
        }
    ]
    snapshot = {
        "runs": [
            {
                "name": "tests",
                "id": 1,
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "sql-integration",
                "id": 2,
                "status": "in_progress",
                "conclusion": None,
                "html_url": "https://example.test/integration",
            },
        ],
        "jobs": [
            {
                "workflow_run_id": 2,
                "name": "core SQL integration (HTTP)",
                "status": "in_progress",
                "conclusion": None,
                "html_url": "https://example.test/integration/job",
            }
        ],
        "check_runs": [
            {
                "name": "core SQL integration (HTTP)",
                "status": "in_progress",
                "conclusion": None,
            }
        ],
        "statuses": [],
    }

    result = mcp_server._classify_github_snapshot(expected, snapshot, advisory=advisory)

    assert result["missing"] == []
    assert result["pending"] == []
    assert result["failed"] == []
    assert result["advisory"][0]["status"] == "in_progress"
    assert result["advisory"][0]["jobs"][0]["status"] == "in_progress"


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
