"""Codex and Claude Code stay synchronized from agent_tools/agent_runtimes.py (offline)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from agent_tools import agent_runtimes, local_review, plan_terminal
from tests._support.paths import REPO_ROOT
from tests.agent_tools.bootstrap_shell import run_wrapper

MCP_FIXTURE = REPO_ROOT / "tests/repository/_support/fixtures/claude_mcp.json"
SESSION_DOCS = (
    "AGENTS.md",
    "agent_docs/development.md",
    "agent_tools/README.md",
    "docs/FEATURE_SESSIONS.md",
)
PAIRED_TOKENS = (
    "bare `{}`",
    "{} --resume-bootstrap",
    "{} --refresh-bootstrap-cache",
)
SOURCE_TOOLS = [
    "mcp__source_files__changed_files",
    "mcp__source_files__list_files",
    "mcp__source_files__read_file",
    "mcp__source_files__read_base_file",
    "mcp__source_files__read_diff",
    "mcp__source_files__read_image",
]
# Exact Claude Code reviewer policy verified empirically with 2.1.310 (see REVIEW_POLICY).
CLAUDE_POLICY = {
    "fresh_session": (("-p",), ("--no-session-persistence",)),
    "ignore_user_configuration": (("--restricted",),),
    "no_builtin_tools": (("--tools", ""),),
    "no_extensions": (("--disable-slash-commands",),),
    "no_network_tools": (("--tools", ""),),
    "read_only_sandbox": (("--restricted",),),
    "no_approval_prompts": (("--permission-mode", "dontAsk"), ("--permission-prompts", "none")),
    "subscription_auth": (),
    "only_source_server": (("--strict-mcp-config",), ("--allowedTools", ",".join(SOURCE_TOOLS))),
    "structured_verdict": (("--json-schema",), ("--output-format", "stream-json", "--verbose")),
}
CODEX_POLICY = {
    "fresh_session": (("exec",), ("--ephemeral",)),
    "ignore_user_configuration": (
        ("--ignore-user-config",),
        ("--ignore-rules",),
        ("-c", "hooks={}"),
    ),
    "no_builtin_tools": (
        ("--disable", "shell_tool"),
        ("--disable", "unified_exec"),
        ("--disable", "shell_snapshot"),
    ),
    "no_extensions": (
        ("--disable", "plugins"),
        ("--disable", "apps"),
        ("--disable", "skill_mcp_dependency_install"),
    ),
    "no_network_tools": (("-c", 'web_search="disabled"'),),
    "read_only_sandbox": (("-s", "read-only"),),
    "no_approval_prompts": (("-c", 'approval_policy="never"'),),
    "subscription_auth": (("-c", 'forced_login_method="chatgpt"'),),
    "only_source_server": (("-c", "mcp_servers.source_files.required=true"),),
    "structured_verdict": (("--output-schema",), ("--json",)),
}


def test_committed_codex_config_is_rendered_from_the_runtime_source() -> None:
    committed = (REPO_ROOT / agent_runtimes.CODEX_CONFIG_PATH).read_text(encoding="utf-8")
    assert committed == agent_runtimes.render_codex_config()


def test_claude_mcp_json_matches_fixture_and_committed_file() -> None:
    assert agent_runtimes.render_mcp_json() == MCP_FIXTURE.read_text(encoding="utf-8")
    codex = agent_runtimes.MCP_SERVERS[agent_runtimes.SERVER_NAME]
    claude = json.loads(MCP_FIXTURE.read_text())["mcpServers"][agent_runtimes.SERVER_NAME]
    assert (claude["command"], claude["args"], claude["env"]) == (
        codex["command"],
        codex["args"],
        codex["env"],
    )
    committed = REPO_ROOT / agent_runtimes.MCP_JSON_PATH
    if not committed.exists():
        pytest.skip("PROTECTED-FOLLOW-UP: .mcp.json is added by a separate owner-approved PR")
    assert committed.read_text(encoding="utf-8") == agent_runtimes.render_mcp_json()


def test_launcher_mcp_config_binds_the_absolute_clone(tmp_path: Path) -> None:
    rendered = json.loads(agent_runtimes.render_mcp_json(tmp_path))
    server = rendered["mcpServers"][agent_runtimes.SERVER_NAME]
    assert server == {
        "type": "stdio",
        "command": str(tmp_path.resolve() / "agent_tools/mcp_tool.sh"),
        "args": [],
        "env": {},
    }
    timeouts = agent_runtimes.MCP_SERVERS[agent_runtimes.SERVER_NAME]
    assert agent_runtimes.claude_launch_env() == {
        "MCP_TOOL_TIMEOUT": str(timeouts["tool_timeout_sec"] * 1000),
        "MCP_TIMEOUT": str(timeouts["startup_timeout_sec"] * 1000),
    }


def test_claude_memory_only_imports_agents_md() -> None:
    assert (REPO_ROOT / "CLAUDE.md").read_bytes() == agent_runtimes.CLAUDE_MEMORY.encode()
    assert agent_runtimes.CLAUDE_MEMORY == "@AGENTS.md\n"


def test_agents_runtime_table_is_generated() -> None:
    text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert text.count(agent_runtimes.TABLE_START) == text.count(agent_runtimes.TABLE_END) == 1
    start = text.index(agent_runtimes.TABLE_START)
    end = text.index(agent_runtimes.TABLE_END) + len(agent_runtimes.TABLE_END) + 1
    assert text[start:end] == agent_runtimes.render_runtime_table()


def test_review_policy_covers_both_runtimes_with_exact_flags() -> None:
    assert set(agent_runtimes.RUNTIMES) == {"codex", "claude"}
    assert {name: value["claude"] for name, value in agent_runtimes.REVIEW_POLICY.items()} == (
        CLAUDE_POLICY
    )
    assert {name: value["codex"] for name, value in agent_runtimes.REVIEW_POLICY.items()} == (
        CODEX_POLICY
    )
    for capability, flags in agent_runtimes.REVIEW_POLICY.items():
        assert set(flags) == {"codex", "claude"}, capability


def _reviewer_args(runtime: str, tmp_path: Path) -> tuple[list[str], Path]:
    schema = REPO_ROOT / ".github/agent/review.schema.json"
    provider = ["/python", "-I", "/job/source_provider.py", "--root", "/clone"]
    args = agent_runtimes.RUNTIMES[runtime].reviewer_argv(
        "/bin/" + runtime, tmp_path, provider, schema, "11111111-1111-4111-8111-111111111111"
    )
    return args, schema


@pytest.mark.parametrize("runtime", ["codex", "claude"])
def test_reviewer_argv_satisfies_every_capability_and_shares_the_schema(
    runtime: str, tmp_path: Path
) -> None:
    args, schema = _reviewer_args(runtime, tmp_path)
    assert agent_runtimes.policy_violations(runtime, args) == []
    if runtime == "claude":
        assert args[args.index("--json-schema") + 1].encode() == schema.read_bytes()
        config = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())
        assert list(config["mcpServers"]) == ["source_files"]
        assert config["mcpServers"]["source_files"]["command"] == "/python"
        assert args[args.index("--session-id") + 1] == "11111111-1111-4111-8111-111111111111"
        assert "--resume" not in args
        assert "--continue" not in args
    else:
        assert args[args.index("--output-schema") + 1] == str(schema)
        assert "resume" not in args
    weakened = [flag for flag in args if flag not in {"--restricted", "--ephemeral"}]
    assert agent_runtimes.policy_violations(runtime, weakened)


def test_built_in_codex_reviewer_is_identical_to_the_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_review.shutil, "which", lambda name: "/bin/codex")
    schema = REPO_ROOT / ".github/agent/review.schema.json"
    monkeypatch.setattr(
        local_review, "trusted_runtime", lambda root, job, base: (job / "provider.py", schema)
    )
    subject = {"head": "h" * 40, "base": "b" * 40}
    built_in = local_review.arguments(tmp_path / "clone", tmp_path, subject)
    provider = local_review.provider(
        tmp_path / "provider.py", tmp_path / "clone", subject, tmp_path
    )
    adapter = agent_runtimes.RUNTIMES["codex"].reviewer_argv(
        "/bin/codex", tmp_path, provider, schema, "unused"
    )
    assert built_in == adapter


def test_shared_reviewer_contract_is_byte_identical() -> None:
    assert local_review.REVIEW_PROMPT == agent_runtimes.REVIEW_PROMPT
    assert local_review.REVIEW_TIMEOUT == agent_runtimes.REVIEW_TIMEOUT
    assert local_review.ENV_DENY == agent_runtimes.ENV_DENY_COMMON
    assert local_review.AUTHOR_SESSION_ENV == agent_runtimes.AUTHOR_SESSION_ENV
    assert local_review.RUNTIME_ADAPTER == "agent_tools/agent_runtimes.py"
    assert "only source review" in agent_runtimes.REVIEW_PROMPT
    environ = {"PATH": "/bin", "HOME": "/h", "LC_ALL": "C", "GITHUB_TOKEN": "x", "OTHER": "y"}
    assert agent_runtimes.RUNTIMES["claude"].reviewer_env(environ) == {
        "PATH": "/bin",
        "HOME": "/h",
        "LC_ALL": "C",
    }
    assert "GITHUB_TOKEN" not in agent_runtimes.RUNTIMES["codex"].reviewer_env(environ)


def test_interactive_sessions_share_effort_and_clone() -> None:
    root = Path("/sessions/0123456789abcdef")
    codex = agent_runtimes.RUNTIMES["codex"].interactive_argv("/bin/codex", root)
    claude = agent_runtimes.RUNTIMES["claude"].interactive_argv("/bin/claude", root)
    assert 'model_reasoning_effort="medium"' in codex
    assert claude[claude.index("--effort") + 1] == "medium"
    assert codex[codex.index("-C") + 1] == str(root)
    assert claude[claude.index("--mcp-config") + 1] == str(
        root / agent_runtimes.CLAUDE_LAUNCH_CONFIG
    )
    assert claude[claude.index("--permission-mode") + 1] == "plan"
    assert plan_terminal.agent_runtimes is agent_runtimes


@pytest.mark.skipif(shutil.which("zsh") is None, reason="The shell launchers require zsh")
@pytest.mark.parametrize(
    ("arguments", "inside"),
    [
        ([], True),
        (["--resume-bootstrap", "0123456789abcdef"], True),
        (["--refresh-bootstrap-cache"], True),
        (["exec", "task"], True),
        ([], False),
    ],
)
def test_zsh_wrappers_behave_identically(
    tmp_path: Path, arguments: list[str], inside: bool
) -> None:
    outputs = {}
    for runtime in ("codex", "claude"):
        directory = tmp_path / runtime
        directory.mkdir()
        lines = run_wrapper(directory, runtime, arguments, inside=inside)
        outputs[runtime] = [
            line.replace(str(directory), "<tmp>").replace(runtime, "<runtime>") for line in lines
        ]
    assert outputs["codex"] == outputs["claude"]
    text = (REPO_ROOT / "agent_tools/atk_sessions.zsh").read_text()
    assert re.findall(r"^(\w+)\(\) \{ _atk_launch (\w+) \"\$@\"; \}$", text, re.MULTILINE) == [
        ("codex", "codex"),
        ("claude", "claude"),
    ]


@pytest.mark.parametrize("document", SESSION_DOCS)
def test_session_docs_name_both_runtimes_in_pairs(document: str) -> None:
    text = (REPO_ROOT / document).read_text(encoding="utf-8").lower()
    for token in PAIRED_TOKENS:
        assert text.count(token.format("codex")) == text.count(token.format("claude")), token
