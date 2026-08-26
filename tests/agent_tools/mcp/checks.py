from __future__ import annotations

from tests.agent_tools._support.mcp import (
    Path,
    _command_result,
    _init_git_repo,
    _write_minimal_repo_files,
    json,
    mcp_server,
    pytest,
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


def test_precommit_fingerprint_includes_untracked_file_contents(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    _init_git_repo(root)
    untracked = root / "new_agent_note.txt"
    untracked.write_text("first version\n", encoding="utf-8")

    first = mcp_server._working_tree_fingerprint(root)
    untracked.write_text("second version\n", encoding="utf-8")
    second = mcp_server._working_tree_fingerprint(root)

    assert first != second


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


def test_run_checks_dry_run_and_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")

    dry_run = mcp_server.run_checks(
        area="agent_tools", level="focused", root=str(root), dry_run=True
    )
    assert dry_run["result"]["planned_commands"] == [
        "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/agent_tools tests/repository"
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
