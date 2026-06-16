from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_tools"))

import mcp_server


def test_next_version_carries_four_part_versions() -> None:
    assert mcp_server._increment_version("1.3.6.6") == "1.3.6.7"
    assert mcp_server._increment_version("1.3.6.19") == "1.3.7.0"
    assert mcp_server._increment_version("1.3.19.19") == "1.4.0.0"
    assert mcp_server._increment_version("1.19.19.19") == "2.0.0.0"


@pytest.mark.parametrize("version", ["1.2.3", "1.2.3.20", "1.2.-1.0"])
def test_next_version_rejects_invalid_versions(version: str) -> None:
    with pytest.raises(ValueError):
        mcp_server._increment_version(version)


def test_route_agent_context_combines_task_and_module_docs() -> None:
    result = mcp_server.route_agent_context("implement load table change", module="sql")

    assert result["required_files"] == [
        "AGENTS.md",
        "agent_docs/development.md",
        "agent_docs/sql.md",
    ]
    assert "Use prepare_start" in result["startup_sequence"][0]


def test_route_agent_context_handles_documentation_work() -> None:
    result = mcp_server.route_agent_context("README documentation update")

    assert "agent_docs/documentation.md" in result["required_files"]
    assert "agent_docs/development.md" not in result["required_files"]


def test_recommend_tests_for_sql_requires_precommit() -> None:
    result = mcp_server.recommend_tests("sql")

    assert result["focused_commands"] == [
        "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_sql_connection_config.py tests/test_sql_retries.py tests/test_sql_load_table.py",
    ]
    assert result["required_final_commands"] == ["release_routines/pre_commit_checks.sh"]


def test_recommend_tests_for_documentation_skips_precommit_requirement() -> None:
    result = mcp_server.recommend_tests("mcp", change_type="documentation")

    assert result["focused_commands"] == [
        "PYTHONPYCACHEPREFIX=/tmp/utils_dev_pycache pytest -q tests/test_agent_tools_mcp.py",
    ]
    assert result["required_final_commands"] == []


def test_changelog_status_reads_repo_files() -> None:
    result = mcp_server.changelog_status(root=str(mcp_server.REPO_ROOT))

    assert result["package_version"]
    assert result["latest_changelog"]["version"]
    assert isinstance(result["matches"], bool)


def test_prepare_start_stops_when_git_pull_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_pull(root: Path) -> dict[str, object]:
        return {"ok": False, "returncode": 1, "stdout": "", "stderr": "conflict"}

    monkeypatch.setattr(mcp_server, "_run_git_pull", fake_pull)

    result = mcp_server.prepare_start("implementation", root=str(mcp_server.REPO_ROOT))

    assert result["ok"] is False
    assert result["phase"] == "git_pull"
    assert "stop and report" in result["message"]


def test_create_mcp_server_reports_missing_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "FastMCP", None)

    with pytest.raises(RuntimeError, match="MCP SDK is not installed"):
        mcp_server.create_mcp_server()


def test_mcp_tool_wrapper_prints_json() -> None:
    script = mcp_server.REPO_ROOT / "agent_tools" / "mcp_tool.sh"

    completed = subprocess.run(
        [str(script), "next-version", "--current-version", "1.3.9.13"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "current_version": "1.3.9.13",
        "next_version": "1.3.9.14",
    }
