from __future__ import annotations

import importlib.util
import json
import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / f".github/agent/{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


promotion = load("automation_promotion")
controller = load("controller")


def git(work: Path, *args: str, raw_stdout: bool = False):
    result = subprocess.run(["git", *args], cwd=work, capture_output=True, check=True)
    return result.stdout if raw_stdout else result.stdout.decode().strip()


def source(work: Path, *, separated_edits: bool = False) -> tuple[str, str]:
    git(work, "init", "--initial-branch=fixture")
    git(work, "config", "user.name", "Fixture")
    git(work, "config", "user.email", "fixture@example.test")
    (work / ".github/agent").mkdir(parents=True)
    (work / "analytics_toolkit").mkdir()
    original = "old automation\n" + (
        "# neutral context\n" * 20 + "feature unchanged\n" if separated_edits else ""
    )
    (work / ".github/agent/controller.py").write_text(original)
    (work / "analytics_toolkit/version.py").write_text("released package\n")
    git(work, "add", "--all")
    git(work, "commit", "-m", "Released main")
    base = git(work, "rev-parse", "HEAD")
    candidate = (
        original.replace("feature unchanged", "approved automation")
        if separated_edits
        else "approved automation\n"
    )
    (work / ".github/agent/controller.py").write_text(candidate)
    (work / "analytics_toolkit/version.py").write_text("unreleased package\n")
    git(work, "add", "--all")
    git(work, "commit", "-m", "Feature candidate")
    head = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "--detach", base)
    return base, head


def test_projection_changes_only_approved_automation_and_preserves_release(tmp_path: Path) -> None:
    base, head = source(tmp_path)
    adapter = SimpleNamespace(command=git, policy_path=controller.policy_path)
    adapter.command = lambda *args, cwd, **kwargs: git(cwd, *args[1:], **kwargs)
    assert promotion.project(adapter, tmp_path, head, base, [".github/agent/controller.py"])
    tree = git(tmp_path, "write-tree")
    assert git(tmp_path, "show", tree + ":.github/agent/controller.py") == "approved automation"
    assert git(tmp_path, "show", tree + ":analytics_toolkit/version.py") == "released package"
    with pytest.raises(ValueError, match="Package paths"):
        promotion.project(adapter, tmp_path, head, base, ["analytics_toolkit/version.py"])


def test_newer_main_automation_is_preserved_instead_of_overwritten(tmp_path: Path) -> None:
    base, head = source(tmp_path)
    (tmp_path / ".github/agent/controller.py").write_text("newer main change\n")
    git(tmp_path, "add", "--all")
    git(tmp_path, "commit", "-m", "Independent automation change")
    adapter = SimpleNamespace(policy_path=controller.policy_path)
    adapter.command = lambda *args, cwd, **kwargs: git(cwd, *args[1:], **kwargs)
    with pytest.raises(ValueError, match="Main automation changed"):
        promotion.project(adapter, tmp_path, head, base, [".github/agent/controller.py"])
    assert git(tmp_path, "show", "HEAD:.github/agent/controller.py") == "newer main change"


@pytest.mark.parametrize("denied", [True, False])
def test_activation_requires_actual_owner_approval_and_exact_base_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denied: bool
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    base, head = source(work)
    git(work, "merge", "--no-ff", head, "-m", "Dev merge")
    merge = git(work, "rev-parse", "HEAD")
    state = tmp_path / "automation-activation.json"
    state.write_text(json.dumps({"started": 0, "done": []}))
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    pr = {
        "number": 7,
        "merged": True,
        "merged_at": "2026-10-14T10:00:00Z",
        "base": {"ref": "dev"},
        "head": {"sha": head},
        "merge_commit_sha": merge,
    }
    gh = Mock(repository="owner/repo")
    gh.pull.return_value = pr
    gh.api.return_value = {"object": {"sha": merge}}
    gh.pages.side_effect = lambda path: (
        [pr] if path.startswith("pulls?") else [{"filename": ".github/agent/controller.py"}]
    )
    gh.checks.return_value = [
        {"name": "fast-checks", "conclusion": "success", "app": {"slug": "github-actions"}},
        {
            "name": "agent-review",
            "conclusion": "success",
            "external_id": "wrong-base",
            "app": {"id": 17},
        },
    ]
    calls = []

    def command(*args, cwd, **kwargs):
        calls.append(args)
        if args[:2] == ("git", "fetch"):
            return ""
        return git(cwd, *args[1:], **kwargs)

    adapter = SimpleNamespace(
        command=command,
        policy_path=controller.policy_path,
        authorization_error=Mock(return_value="Owner approval absent" if denied else None),
        task_metadata=Mock(),
        signing_options=Mock(return_value=[]),
    )
    result = promotion.run(adapter, gh, 17, work)
    assert result["status"] == "deferred"
    assert not any(call[:2] == ("git", "push") for call in calls)
    adapter.authorization_error.assert_called_once()
    if denied:
        gh.checks.assert_not_called()
    else:
        assert base == git(work, "rev-parse", head + "^1")


def test_first_activation_does_not_replay_historical_prs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    gh = Mock(repository="owner/repo")
    gh.pages.return_value = [{"number": 7, "merged_at": "2000-01-01T00:00:00Z"}]
    adapter = SimpleNamespace(command=Mock(), policy_path=controller.policy_path)
    assert promotion.run(adapter, gh, 17, tmp_path)["status"] == "idle"
    gh.pull.assert_not_called()
    adapter.command.assert_not_called()


def merged_source(work: Path, mode: str, *, corrupt: bool) -> tuple[str, str, str]:
    _, head = source(work, separated_edits=True)
    path = work / ".github/agent/controller.py"
    path.write_text(path.read_text().replace("old automation", "earlier approved automation"))
    (work / "independent-dev.txt").write_text("Unrelated dev change must survive squash\n")
    git(work, "add", "--all")
    git(work, "commit", "-m", "Dev advanced independently")
    previous = git(work, "rev-parse", "HEAD")
    if mode == "squash":
        git(work, "merge", "--squash", head)
        git(work, "commit", "-m", "Reviewed dev squash")
    else:
        git(work, "merge", "--no-ff", head, "-m", "Reviewed dev merge")
    if corrupt:
        (work / ".github/agent/controller.py").write_text("Unapproved merged content\n")
        git(work, "add", "--all")
        git(work, "commit", "--amend", "-m", "Forged merge result")
    merge = git(work, "rev-parse", "HEAD")
    return head, previous, merge


@pytest.mark.parametrize(
    ("mode", "late_veto", "corrupt"),
    [
        ("merge", False, False),
        ("merge", True, False),
        ("squash", False, False),
        ("squash", True, False),
        ("squash", False, True),
    ],
)
def test_real_merged_activation_rechecks_owner_before_push(
    tmp_path: Path, monkeypatch, mode, late_veto, corrupt
):
    work = tmp_path / "work"
    work.mkdir()
    head, previous, merge = merged_source(work, mode, corrupt=corrupt)
    git(work, "branch", "main", previous)
    git(work, "branch", "dev", merge)
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--bare", str(work), str(remote))
    git(work, "remote", "add", "origin", str(remote))
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    (tmp_path / "automation-activation.json").write_text(json.dumps({"started": 0, "done": []}))
    pr = {
        "number": 7,
        "merged": True,
        "merged_at": "2026-10-14T10:00:00Z",
        "base": {"ref": "dev"},
        "head": {"sha": head},
        "merge_commit_sha": merge,
    }
    gh = Mock(repository="owner/repo")
    gh.pull.return_value = pr
    gh.pages.side_effect = lambda path: (
        [pr] if path.startswith("pulls?") else [{"filename": ".github/agent/controller.py"}]
    )
    gh.api.side_effect = lambda path: {
        "object": {"sha": git(remote, "rev-parse", "refs/heads/" + path.rsplit("/", 1)[1])}
    }
    gh.checks.return_value = [
        {"name": "fast-checks", "conclusion": "success", "app": {"slug": "github-actions"}},
        {
            "name": "agent-review",
            "conclusion": "success",
            "external_id": previous,
            "app": {"id": 17},
        },
    ]

    def command(*args, cwd=None, **kwargs):
        arguments = list(args[1:])
        if arguments[:3] == ["remote", "set-url", "origin"]:
            arguments[-1] = str(remote)
        # Production signs through the installed machine key. The disposable
        # repository exercises the same publication path without a private key.
        if arguments[0] == "commit-tree":
            arguments.remove("-S")
            arguments = [
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                *arguments,
            ]
        return git(cwd or tmp_path, *arguments, **kwargs)

    adapter = SimpleNamespace(
        command=command,
        policy_path=controller.policy_path,
        authorization_error=Mock(side_effect=[None, "Owner veto" if late_veto else None]),
        task_metadata=Mock(return_value={"digest": "immutable-task"}),
        signing_options=Mock(return_value=[]),
    )
    result = promotion.run(adapter, gh, 17, work)
    main = git(remote, "rev-parse", "main")
    assert result["status"] == ("deferred" if late_veto or corrupt else "activated")
    assert adapter.authorization_error.call_count == (1 if corrupt else 2)
    if late_veto or corrupt:
        assert main == previous
    else:
        promoted = git(remote, "show", "main:.github/agent/controller.py")
        assert promoted.startswith("earlier approved automation\n")
        assert promoted.endswith("approved automation")
        assert git(remote, "show", "main:analytics_toolkit/version.py") == "released package"
        assert git(remote, "rev-parse", "main^1") == previous
    assert not list(tmp_path.glob("automation-activation-*"))
