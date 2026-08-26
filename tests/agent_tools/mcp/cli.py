from __future__ import annotations

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
        def __init__(self, name: str) -> None:
            self.name = name
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

    def fake_factory(name: str) -> FakeMCP:
        server = FakeMCP(name)
        created.append(server)
        return server

    monkeypatch.setattr(mcp_server, "FastMCP", fake_factory)

    mcp_server.create_mcp_server()

    assert created[0].tools == [
        "prepare_start",
        "docs",
        "workflow_status",
        "workflow_metrics",
        "change_impact",
        "version_bump",
        "run_checks",
        "git_workflow",
        "release_workflow",
    ]


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
