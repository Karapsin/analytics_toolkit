from __future__ import annotations

import asyncio
import os
import sys
import warnings
from contextlib import AsyncExitStack

from tests.agent_tools._support.mcp import (
    Path,
    _write_minimal_repo_files,
    _write_unreleased_changelog,
    json,
    mcp_server,
    pytest,
    subprocess,
)


def test_cli_call_returns_nonzero_for_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = mcp_server._build_cli_parser()
    args = parser.parse_args(["run-checks", "--area", "general", "--level", "integration"])
    monkeypatch.setattr(mcp_server, "_build_cli_parser", lambda: parser)
    monkeypatch.setattr(parser, "parse_args", lambda _argv: args)

    assert mcp_server._handle_cli_call([]) == 1


def test_create_mcp_server_exposes_only_consolidated_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMCP:
        def __init__(self, name: str, *, instructions: str) -> None:
            self.name = name
            self.instructions = instructions
            self.tools: list[str] = []
            self.resources: list[str] = []

        def tool(self):
            def register(func):
                self.tools.append(func.__name__)
                return func

            return register

        def resource(self, uri: str):
            def register(func):
                self.resources.append(uri)
                return func

            return register

    created: list[FakeMCP] = []

    def fake_factory(name: str, *, instructions: str) -> FakeMCP:
        server = FakeMCP(name, instructions=instructions)
        created.append(server)
        return server

    monkeypatch.setattr(mcp_server, "MCPServer", fake_factory)

    mcp_server.create_mcp_server()

    assert "prepare_start" in created[0].instructions
    assert created[0].tools == [
        "prepare_start",
        "docs",
        "workflow_status",
        "workflow_metrics",
        "change_impact",
        "version_bump",
        "run_checks",
        "visual_workflow",
        "visual_review",
        "git_workflow",
        "release_workflow",
    ]


def test_mcp_v2_stdio_server_exposes_consolidated_contract() -> None:
    if mcp_server.MCPServer is None:
        pytest.skip("agent-only MCP 2 dependency is not installed")

    from mcp import ClientSession, StdioServerParameters  # noqa: PLC0415
    from mcp.client.stdio import stdio_client  # noqa: PLC0415

    async def exercise() -> None:
        environment = dict(os.environ)
        environment["MCP_PYTHON"] = sys.executable
        parameters = StdioServerParameters(
            command="sh",
            args=[
                "-c",
                'repo_root="$(git rev-parse --show-toplevel)" '
                '&& exec "$repo_root/agent_tools/mcp_tool.sh"',
            ],
            env=environment,
            cwd=mcp_server.REPO_ROOT / "tests",
        )
        async with AsyncExitStack() as stack:
            read_stream, write_stream = await stack.enter_async_context(stdio_client(parameters))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            tools = await session.list_tools()
            resources = await session.list_resources()

        assert [tool.name for tool in tools.tools] == [
            "prepare_start",
            "docs",
            "workflow_status",
            "workflow_metrics",
            "change_impact",
            "version_bump",
            "run_checks",
            "visual_workflow",
            "visual_review",
            "git_workflow",
            "release_workflow",
        ]
        assert "repo://AGENTS.md" in {str(resource.uri) for resource in resources.resources}

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        asyncio.run(exercise())


def test_project_codex_config_registers_portable_mcp_launcher() -> None:
    config = (mcp_server.REPO_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8")

    assert "[mcp_servers.analytics_toolkit_agent_tools]" in config
    assert "git rev-parse --show-toplevel" in config
    assert "required = false" in config
    assert "tool_timeout_sec = 7200" in config


def test_mcp_tool_wrapper_routes_force_release_flag(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    _write_unreleased_changelog(root, ["Existing change"])
    script = mcp_server.REPO_ROOT / "agent_tools" / "mcp_tool.sh"

    completed = subprocess.run(
        [
            str(script),
            "version-bump",
            "--change-type",
            "release",
            "--force-release",
            "--dry-run",
            "--root",
            str(root),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    output = json.loads(completed.stdout)
    assert output["ok"] is True
    assert output["input"]["force_release"] is True
    assert output["result"]["decision"] == "bump"


def test_mcp_tool_wrapper_uses_consolidated_cli_names() -> None:
    script = mcp_server.REPO_ROOT / "agent_tools" / "mcp_tool.sh"

    completed = subprocess.run(
        [str(script), "version-bump", "Wrapper dry run", "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )

    output = json.loads(completed.stdout)
    assert output["ok"] is True
    assert output["tool"] == "version_bump"
    changelog = (mcp_server.REPO_ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    expected_decision = (
        "bump"
        if mcp_server._count_unreleased_changelog_bullets(changelog) + 1
        >= mcp_server.UNRELEASED_CHANGELOG_THRESHOLD
        else "unreleased"
    )
    assert output["result"]["decision"] == expected_decision
