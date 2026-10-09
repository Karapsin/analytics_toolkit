from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest
from agent_tools.plan_terminal import Bootstrap

from agent_tools import install_sessions, mcp_server, sessions, setup_github_app
from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-b", "dev")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.test")
    (root / "seed").write_text("initial\n")
    git(root, "add", "seed")
    git(root, "commit", "-m", "Initial")
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "-u", "origin", "dev")
    return root


def planning(source: Path, tmp_path: Path) -> Path:
    root = sessions.create(source, tmp_path / "sessions")
    state = sessions.load(root)
    state["phase"] = "planning"
    sessions.save(root, state)
    return root


def test_independent_clones_start_on_dev_and_preserve_source_work(
    source: Path, tmp_path: Path
) -> None:
    (source / "seed").write_text("unfinished work\n")
    first = planning(source, tmp_path)
    second = planning(source, tmp_path)
    assert first != second
    assert git(first, "branch", "--show-current") == "dev"
    assert git(second, "branch", "--show-current") == "dev"
    assert (first / "seed").read_text() == "initial\n"
    assert (source / "seed").read_text() == "unfinished work\n"
    branch = sessions.start(first)["branch"]
    assert branch.startswith("codex/")
    assert git(second, "branch", "--show-current") == "dev"


def test_refresh_requires_plan_revalidation_before_branch(source: Path, tmp_path: Path) -> None:
    root = planning(source, tmp_path)
    (source / "seed").write_text("incoming\n")
    git(source, "commit", "-am", "Incoming")
    git(source, "push", "origin", "dev")
    receipt = sessions.start(root)
    assert receipt["status"] == "revalidate"
    assert "seed" in receipt["changes"]
    assert git(root, "branch", "--show-current") == "dev"
    with pytest.raises(RuntimeError, match="Acknowledgement"):
        sessions.start(root, "invalid")
    assert sessions.start(root, receipt["after"])["status"] == "feature"


def test_dirty_and_unfinished_operation_guards(source: Path, tmp_path: Path) -> None:
    root = planning(source, tmp_path)
    (root / "new").write_text("preserve")
    with pytest.raises(RuntimeError, match="dirty"):
        sessions.start(root)
    (root / "new").unlink()
    (root / ".git/MERGE_HEAD").write_text("unfinished")
    with pytest.raises(RuntimeError, match="Unfinished"):
        sessions.start(root)


def test_native_plan_bootstrap_waits_for_menu_then_verifies_mode() -> None:
    bootstrap = Bootstrap()
    assert bootstrap.feed(b"loading; Ask Codex to do anything") == b""
    assert bootstrap.feed(b"GPT-6.1-Sol high") == b""
    assert bootstrap.feed(b"Tip: Startup completed; GPT-6.1-Sol medium") == b"/plan"
    assert bootstrap.feed(b"/plan") == b""
    assert bootstrap.feed(b"switch to Plan mode") == b"\r"
    assert bootstrap.stage == "verify"
    assert bootstrap.feed(b"Model changed to gpt-6.1-sol medium for Plan mode.") == b""
    assert bootstrap.stage == "verify"
    assert bootstrap.feed(b"Ask Codex to do anything") == b""
    assert bootstrap.feed(b"GPT-6.1-Sol xhigh \x1b[13;1HPlan mode") == b""
    assert bootstrap.stage == "ready"


def test_native_plan_bootstrap_accepts_its_own_new_clone_before_plan() -> None:
    bootstrap = Bootstrap()
    assert bootstrap.feed(b"Folder access: Trust and continue") == b"\r"
    assert bootstrap.stage == "loading"
    assert bootstrap.feed(b"Folder access: Trust and continue") == b""
    assert bootstrap.feed(b"Tip: Startup completed; GPT-6.1-Sol medium") == b"/plan"


def test_installer_preserves_symlink_and_is_idempotent(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked.zshrc"
    tracked.write_text("# original\n")
    (tmp_path / ".zshrc").symlink_to(tracked)
    install_sessions.install(REPO_ROOT, tmp_path)
    install_sessions.install(REPO_ROOT, tmp_path)
    assert (tmp_path / ".zshrc").is_symlink()
    assert tracked.read_text().count("source ") == 1
    assert tracked.read_text().startswith("# original\n")


def test_feature_commit_is_blocked_during_bot_ownership(monkeypatch: Any, tmp_path: Path) -> None:
    state = {"phase": "feature", "branch": "codex/owned", "pr": 1}
    monkeypatch.setattr(sessions, "load", lambda root: state)
    monkeypatch.setattr(sessions, "run", lambda *args: "codex/owned")
    monkeypatch.setattr(
        sessions, "pr_info", lambda root: {"state": "OPEN", "labels": [{"name": "agent:writing"}]}
    )
    with pytest.raises(RuntimeError, match="owns this branch"):
        sessions.writable(tmp_path)


def test_feature_watch_never_calls_integration_or_dev_watcher(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(sessions, "load", lambda root: {"phase": "feature"})
    monkeypatch.setattr(sessions, "feedback", lambda root, sha: {"status": "pending", "sha": sha})
    monkeypatch.setattr(
        mcp_server,
        "_watch_github_checks",
        lambda *args, **kwargs: pytest.fail("legacy watcher called"),
    )
    receipt = mcp_server._watch_pushed_commit(
        tmp_path, sha="a" * 40, timeout_seconds=1, wait_seconds=1
    )
    assert receipt["result"]["status"] == "pending"


def test_fragment_does_not_modify_shared_metadata(source: Path, tmp_path: Path) -> None:
    root = planning(source, tmp_path)
    sessions.start(root)
    receipt = sessions.fragment(root, "Added a feature")
    assert (root / receipt["path"]).read_text() == "- Added a feature\n"
    assert not (root / "docs/CHANGELOG.md").exists()
    assert json.loads((root / ".git/atk-session.json").read_text())["phase"] == "feature"


def test_github_manifest_has_both_required_urls_even_with_webhooks_disabled() -> None:
    manifest = setup_github_app.app_manifest(57802)
    assert manifest["url"] == "https://github.com/Karapsin/analytics_toolkit"
    assert manifest["hook_attributes"] == {"url": manifest["url"], "active": False}
    assert manifest["redirect_url"] == "http://127.0.0.1:57802/callback"
