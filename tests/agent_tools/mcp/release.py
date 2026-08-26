from __future__ import annotations

from tests.agent_tools._support.mcp import (
    Path,
    _write_minimal_repo_files,
    mcp_server,
    pytest,
)


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


def test_release_status_blocks_when_exhaustive_integration_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    monkeypatch.setattr(
        mcp_server,
        "_release_readiness",
        lambda root_path, require_release_check=False: {
            "blockers": [],
            "repo_health": {"branch": "main"},
            "command_results": [],
        },
    )
    monkeypatch.setattr(
        mcp_server,
        "_record_release_check_success",
        lambda *_args, **_kwargs: pytest.fail("failed integration must not be recorded"),
    )

    def fake_run_command(root_path: Path, command: dict[str, object]) -> dict[str, object]:
        display = str(command["display"])
        is_integration = display.startswith("python -m release_routines.sql_integration")
        return {
            "ok": not is_integration,
            "command": display,
            "returncode": 1 if is_integration else 0,
            "stdout": "",
            "stderr": "integration failed" if is_integration else "",
            "summary": "integration failed" if is_integration else "ok",
        }

    monkeypatch.setattr(mcp_server, "_run_command", fake_run_command)

    result = mcp_server.release_workflow("status", root=str(root))

    assert result["ok"] is False
    assert result["summary"] == "Release validation command failed."
    assert result["blockers"][0]["phase"] == "release_checks"
    assert "sql_integration" in result["blockers"][0]["command"]


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
        "python -m release_routines.sql_integration --profile all --clickhouse-driver both",
    ]


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
