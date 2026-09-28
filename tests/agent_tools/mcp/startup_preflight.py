from __future__ import annotations

from tests.agent_tools._support.mcp import (
    Path,
    _command_result,
    _git,
    _init_git_repo,
    _write_minimal_repo_files,
    mcp_server,
    pytest,
)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = _write_minimal_repo_files(tmp_path / "project")
    _init_git_repo(root)
    return root


@pytest.mark.parametrize("state", ["staged", "unstaged", "untracked", "conflict"])
def test_startup_rejects_local_changes_before_sync(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    if state == "conflict":
        _git(checkout, "checkout", "-b", "other")
        (checkout / "README.md").write_text("other\n", encoding="utf-8")
        _git(checkout, "add", "README.md")
        _git(
            checkout,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "Other",
        )
        _git(checkout, "checkout", "-b", "work", "HEAD~1")
        (checkout / "README.md").write_text("work\n", encoding="utf-8")
        _git(checkout, "add", "README.md")
        _git(
            checkout,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "Work",
        )
        result = mcp_server._run_git(
            checkout,
            ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "merge", "other"],
        )
        assert result["ok"] is False
    else:
        path = checkout / ("new.txt" if state == "untracked" else "README.md")
        path.write_text("local work\n", encoding="utf-8")
        if state == "staged":
            _git(checkout, "add", "README.md")
    before = mcp_server._run_git(checkout, ["status", "--porcelain=v1"])["stdout"]
    monkeypatch.setattr(
        mcp_server, "_prepare_sync_commands", lambda root: pytest.fail("sync reached")
    )

    result = mcp_server.prepare_start("planning", root=str(checkout))

    assert result["ok"] is False
    assert result["result"]["phase"] == "startup_preflight"
    assert "Checkout has" in result["blockers"][0]["message"]
    assert mcp_server._run_git(checkout, ["status", "--porcelain=v1"])["stdout"] == before


@pytest.mark.parametrize(
    "marker",
    [
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "rebase-merge",
        "rebase-apply",
        "sequencer",
        "BISECT_LOG",
    ],
)
def test_startup_rejects_unfinished_operations_on_clean_tree(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    (checkout / ".git" / marker).touch()
    monkeypatch.setattr(
        mcp_server, "_prepare_sync_commands", lambda root: pytest.fail("sync reached")
    )
    result = mcp_server.prepare_start("planning", root=str(checkout))
    assert result["ok"] is False
    assert result["blockers"][0]["operations"] == [marker]


def test_clean_startup_ignores_agent_cache_and_reaches_sync(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (checkout / ".rag_index").mkdir()
    (checkout / ".rag_index" / "cache").write_text("ignored", encoding="utf-8")
    calls = []

    def stop_at_sync(root: Path) -> list:
        calls.append(root)
        message = "sync reached"
        raise RuntimeError(message)

    monkeypatch.setattr(mcp_server, "_prepare_sync_commands", stop_at_sync)
    with pytest.raises(RuntimeError, match="sync reached"):
        mcp_server.prepare_start("planning", root=str(checkout))
    assert calls == [checkout]


@pytest.mark.parametrize("failed_command", ["status", "rev-parse"])
def test_startup_fails_closed_when_git_state_cannot_be_read(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, failed_command: str
) -> None:
    def run_git(root: Path, args: list[str]) -> dict:
        return _command_result(
            "git " + " ".join(args),
            "",
            ok=args[0] != failed_command,
            stderr="cannot inspect repository" if args[0] == failed_command else "",
        )

    monkeypatch.setattr(mcp_server, "_run_git", run_git)
    monkeypatch.setattr(
        mcp_server, "_prepare_sync_commands", lambda root: pytest.fail("sync reached")
    )
    result = mcp_server.prepare_start("planning", root=str(checkout))
    assert result["ok"] is False
    assert result["blockers"][0]["phase"] == "startup_preflight"


def test_preflight_uses_worktree_git_directory(checkout: Path) -> None:
    linked = checkout.parent / "linked"
    _git(checkout, "worktree", "add", "-b", "linked", str(linked))
    git_dir = mcp_server._run_git(linked, ["rev-parse", "--absolute-git-dir"])["stdout"].strip()
    (Path(git_dir) / "sequencer").mkdir()
    blocker = mcp_server._startup_preflight(linked, [])
    assert blocker is not None
    assert blocker["operations"] == ["sequencer"]
