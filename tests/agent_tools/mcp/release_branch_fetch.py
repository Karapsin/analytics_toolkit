from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from release_routines.lib import release_candidate as candidate

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("problem", ["none", "fetch", "sha", "tree", "before-push", "after-push"])
def test_squash_merged_deleted_branch_is_fetched_before_test_branch(  # noqa: PLR0915 - restart simulation.
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    problem: str,
) -> None:
    state = {
        "pr": 12,
        "version": "1.2.3.4",
        "candidate_sha": "original-pr-head",
        "candidate_tree": "reviewed-tree",
        "workspace": str(tmp_path),
    }
    calls = []
    fetched = []
    remote = []
    interrupted = []
    metadata = '[project]\nname = "analytics-toolkit"\nversion = "1.2.3.4"'
    monkeypatch.setattr(candidate.release_preparation, "state_path", lambda root: tmp_path / "state.json")
    original_save = candidate.release_preparation.save

    def save(root, value):
        original_save(root, value)
        if problem == "before-push" and value.get("testpypi_push_pending") and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt

    monkeypatch.setattr(candidate.release_preparation, "save", save)

    def run(root, *args):  # noqa: C901, PLR0912 - bounded fake Git transcript.
        calls.append(args)
        if args[:3] == ("git", "ls-remote", "--heads"):
            return "test-head refs/heads/testpypi-1.2.3.4" if remote else ""
        if args[:2] == ("git", "push"):
            saved = json.loads((tmp_path / "state.json").read_text())
            assert saved["testpypi_sha"] == "test-head"
            assert saved["testpypi_push_pending"]
            assert any(call[:2] == ("git", "update-ref") for call in calls)
            remote.append("test-head")
            if problem == "after-push" and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt
        if args == ("git", "rev-list", "--parents", "-n", "1", "test-head"):
            return "test-head original-pr-head"
        if args[:2] == ("git", "show"):
            return (
                candidate.replace_project_name_text(metadata, candidate.PROJECTS["testpypi"]).strip()
                if args[2].startswith("test-head:") else metadata
            )
        if args[:2] == ("git", "diff"):
            return "pyproject.toml"
        if args == ("git", "rev-parse", candidate._test_local_ref(state)):
            return "test-head"
        if args[:2] == ("git", "clone"):
            # The default branch contains a squash commit, not the original head.
            assert not fetched
        if args == ("git", "fetch", "origin", "refs/pull/12/head"):
            if problem == "fetch":
                message = "fetch failed"
                raise RuntimeError(message)
            fetched.append("original-pr-head")
        if args == ("git", "rev-parse", "FETCH_HEAD"):
            assert fetched
            return "wrong-head" if problem == "sha" else fetched[0]
        if args == ("git", "rev-parse", "FETCH_HEAD^{tree}"):
            assert fetched
            return "wrong-tree" if problem == "tree" else "reviewed-tree"
        if args[:2] == ("git", "switch"):
            assert fetched == ["original-pr-head"]
            assert args[-1] == fetched[0]
        if args == ("git", "rev-parse", "HEAD"):
            return "test-head"
        return ""

    monkeypatch.setattr(candidate.sessions, "run", run)
    monkeypatch.setattr(candidate.sessions, "load", lambda root: {"signing_key": "key"})
    monkeypatch.setattr(candidate.sessions, "save", lambda *args: None)
    monkeypatch.setattr(candidate.sessions, "commit_options", lambda root: [])
    if problem in {"before-push", "after-push"}:
        with pytest.raises(KeyboardInterrupt):
            candidate._test_branch(tmp_path, state)
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["testpypi_push_pending"]
        assert candidate._test_branch(tmp_path, state) == "test-head"
        assert candidate._test_branch(tmp_path, candidate.release_preparation.load(tmp_path)) == "test-head"
        assert len(remote) == 1
        assert sum("commit" in call for call in calls) == 1
        assert "testpypi_push_pending" not in candidate.release_preparation.load(tmp_path)
    elif problem == "none":
        assert candidate._test_branch(tmp_path, state) == "test-head"
        assert calls.index(("git", "fetch", "origin", "refs/pull/12/head")) < next(
            index for index, call in enumerate(calls) if call[:2] == ("git", "switch")
        )
    else:
        with pytest.raises(RuntimeError, match="fetch failed|verified candidate"):
            candidate._test_branch(tmp_path, state)
        assert not any(call[:2] == ("git", "switch") or "push" in call for call in calls)


@pytest.mark.parametrize("incomplete", [False, True])
def test_all_matching_publication_attempts_are_reconciled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    incomplete: bool,
) -> None:
    pages = [
        {
            "total_count": 3 if incomplete else 2,
            "workflow_runs": [
                {
                    "id": 1,
                    "head_sha": "candidate",
                    "status": "completed",
                    "conclusion": "failure",
                },
            ],
        },
        {
            "total_count": 3 if incomplete else 2,
            "workflow_runs": [
                {
                    "id": 2,
                    "head_sha": "candidate",
                    "status": "in_progress",
                    "conclusion": None,
                },
            ],
        },
    ]
    calls = []

    def run(root, *args):
        calls.append(args)
        return json.dumps(pages)

    monkeypatch.setattr(candidate.sessions, "run", run)
    if incomplete:
        with pytest.raises(RuntimeError, match="incomplete"):
            candidate._runs(tmp_path, "testpypi-1.2.3.4", "candidate", "testpypi")
    else:
        runs = candidate._runs(tmp_path, "testpypi-1.2.3.4", "candidate", "testpypi")
        assert [item["databaseId"] for item in runs] == [1, 2]
        assert runs[1]["status"] == "in_progress"
    assert "--paginate" in calls[0]
    assert "head_sha=candidate" in calls[0]
    assert "event=workflow_dispatch" in calls[0]


@pytest.mark.parametrize("entrypoint", ["publish", "replace"])
@pytest.mark.parametrize(
    "problem", ["none", "parent", "merge", "metadata", "extra", "sha", "fetch"],
)
def test_existing_temporary_ref_requires_complete_candidate_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    entrypoint: str,
    problem: str,
) -> None:
    state = {"candidate_sha": "candidate", "version": "1.2.3.4"}
    if problem == "sha":
        state["testpypi_sha"] = "journaled-other"
    metadata = '[project]\nname = "analytics-toolkit"\nversion = "1.2.3.4"'
    temporary = candidate.replace_project_name_text(metadata, candidate.PROJECTS["testpypi"])
    calls = []
    monkeypatch.setattr(candidate.release_preparation, "state_path", lambda root: tmp_path / "state.json")
    monkeypatch.setattr(candidate, "index_files", lambda *args: {})

    def run(root, *args):
        calls.append(args)
        if args[:3] == ("git", "ls-remote", "--heads"):
            return "test-head refs/heads/testpypi-1.2.3.4"
        if args == ("git", "fetch", "origin", "test-head") and problem == "fetch":
            message = "fetch unavailable"
            raise RuntimeError(message)
        if args[:2] == ("git", "rev-list"):
            return {
                "parent": "test-head other",
                "merge": "test-head candidate other",
            }.get(problem, "test-head candidate")
        if args[:2] == ("git", "show"):
            if args[2].startswith("candidate:"):
                return metadata
            return temporary.strip() + ('\ndescription = "changed"' if problem == "metadata" else "")
        if args[:2] == ("git", "diff"):
            return "pyproject.toml\nextra.py" if problem == "extra" else "pyproject.toml"
        return ""

    def runs(root, ref, sha, stage):
        saved = candidate.release_preparation.load(tmp_path)
        assert saved["testpypi_sha"] == "test-head"
        return []

    monkeypatch.setattr(candidate.sessions, "run", run)
    monkeypatch.setattr(candidate, "_runs", runs)
    action = candidate._test_branch if entrypoint == "publish" else candidate.reconcile_replacement
    if problem == "none":
        action(tmp_path, state)
        assert candidate.release_preparation.load(tmp_path)["testpypi_sha"] == "test-head"
        assert sum("push" in call for call in calls) == (entrypoint == "replace")
    else:
        with pytest.raises(RuntimeError, match="candidate|metadata|changes|reconciled|unavailable"):
            action(tmp_path, state)
        assert not any("push" in call or "workflow" in call for call in calls)
        assert not (tmp_path / "state.json").exists()


@pytest.mark.parametrize("entrypoint", ["publish", "replace"])
@pytest.mark.parametrize("problem", ["missing", "retained-mismatch", "push-conflict", "not-visible"])
def test_pending_push_and_established_ref_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, problem: str, entrypoint: str,
) -> None:
    state = {
        "candidate_sha": "candidate", "version": "1.2.3.4", "testpypi_sha": "test-head",
    }
    if problem != "missing":
        state["testpypi_push_pending"] = True
    metadata = '[project]\nname = "analytics-toolkit"\nversion = "1.2.3.4"'
    calls = []

    def run(root, *args):
        calls.append(args)
        if args[:2] == ("git", "rev-parse"):
            return "other" if problem == "retained-mismatch" else "test-head"
        if args[:2] == ("git", "rev-list"):
            return "test-head candidate"
        if args[:2] == ("git", "show"):
            return (
                candidate.replace_project_name_text(metadata, candidate.PROJECTS["testpypi"]).strip()
                if args[2].startswith("test-head:") else metadata
            )
        if args[:2] == ("git", "diff"):
            return "pyproject.toml"
        if args[:2] == ("git", "push"):
            assert "--force-with-lease=refs/heads/testpypi-1.2.3.4:" in args
            if problem == "push-conflict":
                message = "concurrent push conflict"
                raise RuntimeError(message)
        return ""

    monkeypatch.setattr(candidate.sessions, "run", run)
    monkeypatch.setattr(candidate, "index_files", lambda *args: {})
    action = candidate._test_branch if entrypoint == "publish" else candidate.reconcile_replacement
    with pytest.raises(RuntimeError, match="disappeared|journaled|conflict|not visible"):
        action(tmp_path, state)
    assert not any("clone" in call or "workflow" in call for call in calls)
    assert sum("push" in call for call in calls) == (problem in {"push-conflict", "not-visible"})


def test_interruption_after_leased_deletion_reconciles_on_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    state = {"candidate_sha": "candidate", "version": "1.2.3.4", "testpypi_sha": "test-head"}
    metadata = '[project]\nname = "analytics-toolkit"\nversion = "1.2.3.4"'
    remote = ["test-head"]
    lookups = []
    deletions = []
    monkeypatch.setattr(candidate.release_preparation, "state_path", lambda root: tmp_path / "state.json")
    monkeypatch.setattr(candidate, "index_files", lambda *args: {})

    def run(root, *args):
        if args[:3] == ("git", "ls-remote", "--heads"):
            return "test-head refs/heads/testpypi-1.2.3.4" if remote else ""
        if args[:2] == ("git", "rev-list"):
            return "test-head candidate"
        if args[:2] == ("git", "show"):
            return (
                candidate.replace_project_name_text(metadata, candidate.PROJECTS["testpypi"]).strip()
                if args[2].startswith("test-head:") else metadata
            )
        if args[:2] == ("git", "diff"):
            return "pyproject.toml"
        if args[:2] == ("git", "push"):
            saved = candidate.release_preparation.load(tmp_path)
            assert saved["testpypi_removal_pending"] == "test-head"
            assert "--force-with-lease=refs/heads/testpypi-1.2.3.4:test-head" in args
            deletions.append(args)
            remote.clear()
            raise KeyboardInterrupt
        return ""

    def runs(root, ref, sha, stage):
        lookups.append((stage, sha))
        return []

    monkeypatch.setattr(candidate.sessions, "run", run)
    monkeypatch.setattr(candidate, "_runs", runs)
    with pytest.raises(KeyboardInterrupt):
        candidate.reconcile_replacement(tmp_path, state)
    restarted = candidate.release_preparation.load(tmp_path)
    assert not candidate.reconcile_replacement(tmp_path, restarted)
    assert not candidate.reconcile_replacement(tmp_path, restarted)
    assert len(deletions) == 1
    assert lookups.count(("testpypi", "test-head")) == 3
