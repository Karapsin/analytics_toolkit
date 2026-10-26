from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_tools import bootstrap_process, plan_terminal


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("ATK_BOOTSTRAP_CACHE_DIR", raising=False)
    source = tmp_path / "source"
    source.mkdir()
    identity = "0123456789abcdef"
    directory = tmp_path / ".local/share/analytics-toolkit-agent/sessions" / "repository"
    root = directory / identity
    (root / ".git").mkdir(parents=True)
    state = {
        "id": identity,
        "source": str(source),
        "phase": "launching",
        "base": "old",
        "branch": "dev",
    }
    commands, launches, creates = [], [], []
    monkeypatch.setattr(plan_terminal.sessions, "repository_key", lambda source: "repository")
    monkeypatch.setattr(plan_terminal.sessions, "load", lambda root: state.copy())
    monkeypatch.setattr(plan_terminal.sessions, "save", lambda root, value: state.update(value))
    monkeypatch.setattr(plan_terminal.sessions, "clean", lambda root: None)
    monkeypatch.setattr(plan_terminal.sessions, "sync_dev", lambda root: "latest")

    def git(root, *args):
        if args == ("git", "rev-parse", "--show-toplevel"):
            return str(root.resolve())
        if args == ("git", "branch", "--show-current"):
            return "dev"
        assert args == ("git", "rev-parse", "HEAD")
        return "latest"

    def create(source, directory):
        creates.append(directory)
        return root

    def run(args, **kwargs):
        commands.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"ok": True}))

    monkeypatch.setattr(plan_terminal.sessions, "run", git)
    monkeypatch.setattr(plan_terminal.sessions, "create", create)
    monkeypatch.setattr(plan_terminal.machine_signing, "ensure_key", lambda home: tmp_path / "key")
    monkeypatch.setattr(plan_terminal.machine_signing, "configure", lambda *args: None)
    monkeypatch.setattr(plan_terminal.subprocess, "run", run)
    monkeypatch.setattr(
        plan_terminal, "launch", lambda executable, root: launches.append(root) or 0
    )
    monkeypatch.setattr(
        plan_terminal,
        "launch_claude",
        lambda executable, root: launches.append(("claude", root)) or 0,
    )
    return source, root, state, commands, launches, creates


RUNTIMES = pytest.mark.parametrize("runtime", ["codex", "claude"])


def invoke(runtime, source, *args):
    return plan_terminal.main(["--runtime", runtime, str(source), "/" + runtime, *args])


@RUNTIMES
@pytest.mark.parametrize(
    "mode", [[], ["--refresh-bootstrap-cache"], ["--resume-bootstrap", "0123456789abcdef"]]
)
def test_launcher_prepares_once_before_entering_plan(launcher, mode, runtime):
    source, root, state, commands, launches, creates = launcher
    assert invoke(runtime, source, *mode) == 0
    assert len(commands) == 1
    args, options = commands[0]
    assert args[0] == str(root / "agent_tools/mcp_tool.sh")
    assert "prepare-start" in args
    assert options["env"]["MCP_PYTHON"] == plan_terminal.sys.executable
    assert options["env"]["ATK_BOOTSTRAP_PROGRESS"] == "1"
    assert options["env"]["ATK_BOOTSTRAP_REFRESH"] == (
        "1" if mode == ["--refresh-bootstrap-cache"] else "0"
    )
    assert state["phase"] == "planning"
    assert state["base"] == "latest"
    assert state["runtime"] == state["reviewer"] == runtime
    display = plan_terminal.agent_runtimes.RUNTIMES[runtime].display
    assert args[args.index("--task") + 1] == f"New isolated {display} session"
    assert launches == [root if runtime == "codex" else ("claude", root)]
    assert len(creates) == (0 if "--resume-bootstrap" in mode else 1)
    log = root / ".rag_index/bootstrap/launcher.log"
    assert json.loads(log.read_text())["ok"] is True
    assert log.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "receipt",
    [
        ("{}", 0),
        ("[]", 0),
        ("invalid", 0),
        ('{"ok":true}', 1),
        ('{"ok":false}', 1),
        ('{"blockers":[3],"result":7}', 1),
    ],
)
@RUNTIMES
def test_failed_receipt_never_launches_or_marks_planning(
    launcher, monkeypatch, capsys, receipt, runtime
):
    output, code = receipt
    source, root, state, _, launches, _ = launcher
    monkeypatch.setattr(
        plan_terminal.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, code, stdout=output),
    )
    assert invoke(runtime, source) == 1
    assert state["phase"] == "launching"
    assert "runtime" not in state
    assert not launches
    assert f"{runtime} --resume-bootstrap {root.name}" in capsys.readouterr().err
    assert (root / ".rag_index/bootstrap/launcher.log").read_text() == output


@pytest.mark.parametrize(
    ("field", "value"), [("phase", "feature"), ("source", "/other"), ("id", "different")]
)
def test_resume_rejects_other_sessions_and_preserves_them(launcher, field, value):
    source, root, state, commands, launches, creates = launcher
    state[field] = value
    before = state.copy()
    with pytest.raises(RuntimeError, match="launching"):
        plan_terminal.main([str(source), "/codex", "--resume-bootstrap", root.name])
    assert state == before
    assert not commands
    assert not launches
    assert not creates


@pytest.mark.parametrize("identity", ["../outside", "bad", "g" * 16])
def test_resume_rejects_malformed_identity(launcher, identity):
    source, _, _, commands, _, _ = launcher
    with pytest.raises(RuntimeError, match="Invalid"):
        plan_terminal.main([str(source), "/codex", "--resume-bootstrap", identity])
    assert not commands


def test_resume_refuses_dirty_or_unfinished_work(launcher, monkeypatch):
    source, root, _, commands, _, _ = launcher

    def dirty(root):
        msg = "preserve dirty work"
        raise RuntimeError(msg)

    monkeypatch.setattr(plan_terminal.sessions, "clean", dirty)
    with pytest.raises(RuntimeError, match="dirty"):
        plan_terminal.main([str(source), "/codex", "--resume-bootstrap", root.name])
    assert not commands


def test_concurrent_resume_is_rejected(launcher):
    source, root, state, commands, launches, _ = launcher
    with bootstrap_process.lock(root / ".git/atk-bootstrap.lock"):
        assert plan_terminal.main([str(source), "/codex", "--resume-bootstrap", root.name]) == 1
    assert state["phase"] == "launching"
    assert not launches
    assert not commands


def test_interrupted_preparation_can_be_resumed(launcher, monkeypatch, capsys):
    source, _root, state, _, launches, _ = launcher

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(plan_terminal, "_prepare", interrupt)
    assert plan_terminal.main([str(source), "/codex"]) == 130
    assert state["phase"] == "launching"
    assert not launches
    assert "--resume-bootstrap" in capsys.readouterr().err


def test_cache_directory_override_is_supported(launcher, tmp_path, monkeypatch):
    source = launcher[0]
    monkeypatch.setenv("ATK_BOOTSTRAP_CACHE_DIR", str(tmp_path / "custom"))
    assert plan_terminal.bootstrap_env_directory(source) == tmp_path / "custom"


def test_resume_refreshes_clone_before_loading_its_tools(launcher, monkeypatch):
    source, root, _, commands, _, _ = launcher
    order = []
    monkeypatch.setattr(
        plan_terminal.sessions, "sync_dev", lambda root: order.append("sync") or "latest"
    )
    original = plan_terminal.subprocess.run

    def prepared(*args, **kwargs):
        assert order == ["sync"]
        order.append("prepare")
        return original(*args, **kwargs)

    monkeypatch.setattr(plan_terminal.subprocess, "run", prepared)
    assert plan_terminal.main([str(source), "/codex", "--resume-bootstrap", root.name]) == 0
    assert len(commands) == 1
    assert order == ["sync", "prepare"]


@pytest.mark.parametrize("root_mismatch", [True, False])
def test_resume_preserves_unexpected_branch_or_git_root(launcher, monkeypatch, root_mismatch):
    source, root, state, commands, launches, _ = launcher
    original = plan_terminal.sessions.run

    def git(root, *args):
        if args == ("git", "rev-parse", "--show-toplevel") and root_mismatch:
            return "/other/root"
        if args == ("git", "branch", "--show-current"):
            return "feature"
        return original(root, *args)

    monkeypatch.setattr(plan_terminal.sessions, "run", git)
    with pytest.raises(RuntimeError, match=r"Git root|requires dev"):
        plan_terminal.main([str(source), "/codex", "--resume-bootstrap", root.name])
    assert not commands
    assert not launches
    assert state["phase"] == "launching"


def test_resume_rejects_symlink_to_a_clone_outside_session_directory(launcher, tmp_path):
    source, root, _, commands, _, _ = launcher
    moved = tmp_path / "elsewhere"
    root.rename(moved)
    root.symlink_to(moved, target_is_directory=True)
    with pytest.raises(RuntimeError, match="owned clone"):
        plan_terminal.main([str(source), "/codex", "--resume-bootstrap", root.name])
    assert not commands


@pytest.mark.parametrize(
    ("receipt", "expected"),
    [
        (
            {
                "blockers": [{"excerpt": "ReadTimeoutError: traceback"}],
                "result": {"phase": "download"},
            },
            "download: transient network failure",
        ),
        (
            {"blockers": [{"excerpt": "details\nERROR: conflict\nhelp"}]},
            "bootstrap: ERROR: conflict",
        ),
    ],
)
def test_failed_preparation_reports_a_short_cause(launcher, monkeypatch, capsys, receipt, expected):
    source = launcher[0]
    monkeypatch.setattr(
        plan_terminal.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, stdout=json.dumps(receipt)),
    )
    assert plan_terminal.main([str(source), "/codex"]) == 1
    assert expected in capsys.readouterr().err


@RUNTIMES
def test_reviewer_override_is_recorded_and_unknown_reviewer_fails_closed(
    launcher, monkeypatch, runtime
):
    source, _root, state, commands, launches, _ = launcher
    other = "claude" if runtime == "codex" else "codex"
    monkeypatch.setenv("ATK_LOCAL_REVIEWER", other)
    assert invoke(runtime, source) == 0
    assert (state["runtime"], state["reviewer"]) == (runtime, other)
    monkeypatch.setenv("ATK_LOCAL_REVIEWER", "gemini")
    with pytest.raises(RuntimeError, match="Unknown local reviewer"):
        invoke(runtime, source)
    assert len(commands) == len(launches) == 1


def test_claude_launch_binds_absolute_clone_mcp_and_plan_mode(tmp_path, monkeypatch):
    root = tmp_path / "0123456789abcdef"
    root.mkdir()
    calls = []
    monkeypatch.setattr(plan_terminal.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(plan_terminal.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(plan_terminal.os, "chdir", lambda path: calls.append(("chdir", path)))
    monkeypatch.setattr(
        plan_terminal.os, "execve", lambda exe, argv, env: calls.append((exe, argv, env))
    )
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "1")
    plan_terminal.launch_claude("/bin/claude", root)
    assert calls[0] == ("chdir", root)
    executable, argv, environment = calls[1]
    config = root / ".rag_index/bootstrap/claude-mcp.json"
    assert executable == "/bin/claude"
    assert argv[:7] == [
        "/bin/claude",
        "--permission-mode",
        "plan",
        "-n",
        "atk-0123456789abcdef",
        "--mcp-config",
        str(config),
    ]
    assert environment["MCP_TOOL_TIMEOUT"] == "7200000"
    assert environment["MCP_TIMEOUT"] == "30000"
    server = json.loads(config.read_text())["mcpServers"]["analytics_toolkit_agent_tools"]
    assert server["command"] == str(root.resolve() / "agent_tools/mcp_tool.sh")


def test_claude_launch_requires_a_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_terminal.sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(RuntimeError, match="claude -p"):
        plan_terminal.launch_claude("/bin/claude", tmp_path)
