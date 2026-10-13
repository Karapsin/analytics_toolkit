from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from release_routines.lib import release_candidate as candidate

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("problem", ["none", "fetch", "sha", "tree"])
def test_squash_merged_deleted_branch_is_fetched_before_test_branch(
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

    def run(root, *args):
        calls.append(args)
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
    if problem == "none":
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
