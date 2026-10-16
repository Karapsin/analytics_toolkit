from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

import pytest

from agent_tools import machine_signing, mcp_server, session_cleanup, sessions, task_context
from tests.agent_tools.sessions import git, planning
from tests.agent_tools.sessions import source as source  # noqa: PLC0414 - imported pytest fixture.

if TYPE_CHECKING:
    from pathlib import Path


def submitted(source: Path, tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    root = planning(source, tmp_path)
    sessions.start(root)
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.test")
    key = machine_signing.ensure_key(tmp_path)
    machine_signing.configure(root, key)
    task_context.record(root, "Implement the requested feature.")
    (root / "feature").write_text("feature\n")
    git(root, "add", "feature")
    git(
        root,
        *sessions.commit_options(root),
        "commit",
        "-m",
        "Feature" + task_context.commit_trailers(root),
    )
    state = sessions.load(root)
    state.update(pr=12, publication_sha=git(root, "rev-parse", "HEAD"))
    sessions.save(root, state)
    git(root, "push", "origin", state["branch"])
    pr = {
        "number": 12,
        "url": "https://github.test/pull/12",
        "state": "OPEN",
        "mergedAt": None,
        "headRefOid": state["publication_sha"],
        "headRefName": state["branch"],
        "baseRefName": "dev",
        "body": task_context.attachment(root),
        "labels": [],
        "reviews": [],
        "statusCheckRollup": [],
    }
    return root, pr


def no_network(monkeypatch: Any, pr: dict[str, Any]) -> None:
    real = sessions.run
    monkeypatch.setattr(sessions, "pr_info", lambda root, target=None: pr)

    def fake(root: Path, *args: str) -> str:
        if args[0] == "gh":
            if args[1:3] == ("pr", "edit"):
                return ""
            if args[1:3] == ("pr", "list"):
                return "[]"
            pytest.fail("Unexpected GitHub call: " + " ".join(args))
        return real(root, *args)

    monkeypatch.setattr(sessions, "run", fake)


def test_handoff_persists_ownership_and_allows_next_feature_before_merge(
    source: Path,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root, pr = submitted(source, tmp_path)
    first_fragment = sessions.fragment_path(sessions.load(root))
    original = task_context.read(root)
    no_network(monkeypatch, pr)
    result = sessions.handoff(root)
    assert result["status"] == "handed_off"
    assert result["owner"] == "pi"
    assert sessions.load(root)["phase"] == "planning"
    assert git(root, "branch", "--show-current") == "dev"
    assert (task_context.directory(root) / (original["task_id"] + ".json")).is_file()
    assert not (task_context.directory(root) / "active.json").exists()
    with pytest.raises(RuntimeError, match="session-owned"):
        sessions.writable(root)
    task_context.record(root, "A second feature while the first PR is open.")
    next_task = task_context.read(root)["task_id"]
    assert next_task != original["task_id"]
    sessions.synchronize(root)
    assert task_context.read(root)["task_id"] == next_task
    assert sessions.start(root)["status"] == "feature"
    assert sessions.fragment_path(sessions.load(root)) != first_fragment
    git(root, "switch", pr["headRefName"])
    with pytest.raises(RuntimeError, match="session-owned"):
        sessions.writable(root)


def test_push_hands_off_without_check_or_review_calls(
    source: Path,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root, pr = submitted(source, tmp_path)
    no_network(monkeypatch, pr)
    monkeypatch.setattr(sessions, "feedback", lambda *args: pytest.fail("Feedback was queried"))
    monkeypatch.setattr(
        mcp_server, "_watch_github_checks", lambda *args, **kwargs: pytest.fail("CI queried")
    )
    result = sessions.push(root)
    assert result["status"] == "handed_off"
    watch = mcp_server._watch_pushed_commit(
        root, sha=result["sha"], timeout_seconds=1, wait_seconds=1
    )
    assert watch["result"]["status"] == "delegated"


def test_sync_failure_retains_durable_handoff_and_is_recoverable(
    source: Path,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root, pr = submitted(source, tmp_path)
    no_network(monkeypatch, pr)
    real_sync = sessions.sync_dev
    monkeypatch.setattr(
        sessions, "sync_dev", lambda root: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    result = sessions.handoff(root)
    assert result["local_sync_pending"] == "offline"
    assert sessions.load(root)["phase"] == "handed_off"
    with pytest.raises(RuntimeError, match="session-owned"):
        sessions.writable(root)
    monkeypatch.setattr(sessions, "sync_dev", real_sync)
    assert sessions.handoff(root)["status"] == "handed_off"
    assert sessions.load(root)["phase"] == "planning"


@pytest.mark.parametrize("changed", ["attachment", "base", "closed", "unsigned"])
def test_bad_publication_retains_local_ownership(
    source: Path,
    tmp_path: Path,
    monkeypatch: Any,
    changed: str,
) -> None:
    root, pr = submitted(source, tmp_path)
    if changed == "attachment":
        pr["body"] = "No user prompt attached."
    elif changed == "base":
        pr["baseRefName"] = "main"
    elif changed == "closed":
        pr["state"] = "CLOSED"
    else:
        git(root, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "Unsigned")
        state = sessions.load(root)
        state["publication_sha"] = git(root, "rev-parse", "HEAD")
        sessions.save(root, state)
    no_network(monkeypatch, pr)
    with pytest.raises((RuntimeError, ValueError)):
        sessions.handoff(root)
    assert sessions.load(root)["phase"] == "feature"
    assert task_context.read(root)["starting_prompt"] == "Implement the requested feature."


def test_optional_feedback_uses_explicit_pr_without_local_head_requirement(
    source: Path,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root, pr = submitted(source, tmp_path)
    no_network(monkeypatch, pr)
    sessions.handoff(root)
    receipt = sessions.feedback(root, pr["headRefOid"], 12)
    assert receipt["writer"] == "github"
    assert receipt["pr"] == 12
    assert not receipt["refresh_required"]
    with pytest.raises(RuntimeError, match="current PR head"):
        sessions.feedback(root, "0" * 40, 12)


def test_cleanup_preserves_active_unpublished_squash_and_changed_heads(
    source: Path,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root = planning(source, tmp_path)
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.test")
    base = git(root, "rev-parse", "HEAD")
    for branch in ("merged", "unknown", "changed", "active", "squashed", "backup/keep"):
        git(root, "branch", "codex/" + branch)
    git(root, "switch", "codex/squashed")
    git(root, "commit", "--allow-empty", "-m", "Unique squash history")
    squash_head = git(root, "rev-parse", "HEAD")
    git(root, "switch", "dev")
    git(root, "worktree", "add", str(tmp_path / "active"), "codex/active")
    real = sessions.run
    proofs = [
        {"headRefName": "codex/" + name, "headRefOid": head, "number": index}
        for index, (name, head) in enumerate(
            (
                ("merged", base),
                ("changed", "0" * 40),
                ("active", base),
                ("squashed", squash_head),
                ("backup/keep", base),
            )
        )
    ]
    monkeypatch.setattr(
        sessions,
        "run",
        lambda root, *args: json.dumps(proofs) if args[0] == "gh" else real(root, *args),
    )
    receipt = session_cleanup.cleanup(root)
    assert receipt["removed"] == ["codex/merged"]
    assert set(receipt["preserved"]) == {
        "codex/unknown",
        "codex/changed",
        "codex/active",
        "codex/squashed",
        "codex/backup/keep",
    }
    assert git(root, "branch", "--show-current") == "dev"


def test_dirty_cleanup_never_deletes_branches(source: Path, tmp_path: Path) -> None:
    root = planning(source, tmp_path)
    git(root, "branch", "codex/preserve")
    (root / "unpublished").write_text("keep")
    receipt = session_cleanup.cleanup(root)
    assert "dirty" in receipt["pending"]
    assert "codex/preserve" in git(root, "branch", "--list")


def test_sync_preloads_helpers_before_old_dev_removes_the_new_module(
    source: Path,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root = planning(source, tmp_path)
    module_name = "handoff_cleanup_fixture"
    fixture = tmp_path / (module_name + ".py")
    fixture.write_text("def cleanup(root):\n    return {'loaded_before_switch': True}\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    real_import = sessions.importlib.import_module
    real_sync = sessions.sync_dev

    def import_helper(name: str) -> Any:
        if name.endswith("session_cleanup"):
            # Emulate a newly added helper that does not exist in the old dev tree.
            assert fixture.exists()
            return real_import(module_name)
        return real_import(name)

    def switch_to_old_dev(checkout: Path) -> str:
        fixture.unlink()
        return real_sync(checkout)

    monkeypatch.setattr(sessions.importlib, "import_module", import_helper)
    monkeypatch.setattr(sessions, "sync_dev", switch_to_old_dev)
    monkeypatch.setattr(sessions, "_sync_shared", lambda source: None)
    try:
        receipt = sessions.synchronize(root)
        assert receipt["branch_cleanup"] == {"loaded_before_switch": True}
        assert not fixture.exists()
        assert sessions.load(root)["phase"] == "planning"
    finally:
        sys.modules.pop(module_name, None)


def test_failed_push_preserves_feature_ownership(
    source: Path,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root, pr = submitted(source, tmp_path)
    no_network(monkeypatch, pr)
    real = sessions.run

    def fail(root: Path, *args: str) -> str:
        if args[:2] == ("git", "push"):
            msg = "Network unavailable"
            raise RuntimeError(msg)
        return real(root, *args)

    monkeypatch.setattr(sessions, "run", fail)
    with pytest.raises(RuntimeError, match="Network"):
        sessions.push(root)
    assert sessions.load(root)["phase"] == "feature"


@pytest.mark.parametrize("changed", ["hold", "writer", "closed", "head", "task", "session"])
def test_archive_io_rechecks_authority_before_feature_push(
    source: Path, tmp_path: Path, monkeypatch: Any, changed: str
) -> None:
    root, pr = submitted(source, tmp_path)
    no_network(monkeypatch, pr)
    publish = task_context.publish_attachment
    real = sessions.run
    writes = []

    def record(root: Path, *args: str) -> str:
        if args[:2] == ("git", "push") or args[:3] == ("gh", "pr", "edit"):
            writes.append(args)
        return real(root, *args)

    def interrupted(root: Path, *, prefix: str = "") -> str:
        body = publish(root, prefix=prefix)
        if changed in {"hold", "writer"}:
            pr["labels"] = [{"name": "agent:needs-user" if changed == "hold" else "agent:writing"}]
        elif changed == "closed":
            pr["state"] = "CLOSED"
        elif changed == "head":
            git(root, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "New head")
        elif changed == "task":
            task_context.record(root, "A new user correction during archive I/O.", append=True)
        else:
            state = sessions.load(root)
            state["phase"] = "handed_off"
            sessions.save(root, state)
        return body

    monkeypatch.setattr(sessions, "run", record)
    monkeypatch.setattr(task_context, "publish_attachment", interrupted)
    with pytest.raises((RuntimeError, ValueError)):
        sessions.push(root)
    assert writes == []
    assert (root / "feature").read_text() == "feature\n"
