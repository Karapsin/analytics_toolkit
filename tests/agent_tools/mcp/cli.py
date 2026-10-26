from __future__ import annotations

import asyncio
import os
import sys
import warnings
from contextlib import AsyncExitStack
from types import SimpleNamespace

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
            self.read_only: set[str] = set()
            self.resources: list[str] = []

        def tool(self, *, annotations):
            def register(func):
                self.tools.append(func.__name__)
                if annotations.read_only_hint:
                    assert not annotations.destructive_hint
                    self.read_only.add(func.__name__)
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
    monkeypatch.setattr(mcp_server, "ToolAnnotations", SimpleNamespace)

    mcp_server.create_mcp_server()

    assert "prepare_start" in created[0].instructions
    assert created[0].read_only == {"docs", "workflow_status", "workflow_metrics", "change_impact"}
    assert created[0].tools == [
        "prepare_start",
        "task_context",
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
            "task_context",
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
        # Claude Code Plan mode runs only read-only tools without an approval prompt.
        read_only = {
            tool.name
            for tool in tools.tools
            if tool.annotations and tool.annotations.read_only_hint
        }
        assert (
            read_only
            == set(mcp_server.READ_ONLY_TOOLS)
            == {
                "docs",
                "workflow_status",
                "workflow_metrics",
                "change_impact",
            }
        )
        for tool in tools.tools:
            assert tool.annotations is not None
            assert tool.annotations.destructive_hint is (tool.name not in read_only)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        asyncio.run(exercise())


@pytest.mark.parametrize("runtime", ["codex", "claude"])
def test_project_config_registers_portable_mcp_launcher(runtime: str) -> None:
    from agent_tools import agent_runtimes  # noqa: PLC0415

    if runtime == "codex":
        config = (mcp_server.REPO_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert config == agent_runtimes.render_codex_config()
        assert "[mcp_servers.analytics_toolkit_agent_tools]" in config
        assert "required = false" in config
        assert "tool_timeout_sec = 7200" in config
    else:
        config = (mcp_server.REPO_ROOT / ".mcp.json").read_text(encoding="utf-8")
        assert config == agent_runtimes.render_mcp_json()
        server = json.loads(config)["mcpServers"]["analytics_toolkit_agent_tools"]
        assert server["type"] == "stdio"
        assert agent_runtimes.claude_launch_env()["MCP_TOOL_TIMEOUT"] == "7200000"
    assert "git rev-parse --show-toplevel" in config


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
    expected_decision = "unreleased"
    if mcp_server.sessions.load(mcp_server.REPO_ROOT).get("phase") == "feature":
        expected_decision = "fragment"
    assert output["result"]["decision"] == expected_decision
