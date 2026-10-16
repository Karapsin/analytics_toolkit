from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from release_routines.lib import release_candidate as candidate

from agent_tools import release_preparation as preparation
from agent_tools import release_replacement as replacement
from tests._support.clock import FakeClock
from tests.agent_tools._support.mcp import _write_minimal_repo_files, mcp_server

if TYPE_CHECKING:
    from pathlib import Path


def _replacement_metadata(files, args):
    path = args[2].split(":", 1)[1]
    if args[2] == "test-head:pyproject.toml":
        return candidate.replace_project_name_text(
            files[path],
            candidate.PROJECTS["testpypi"],
        ).strip()
    return files[path].strip()


def _replacement_source_git(transcript, *args):
    transcript.calls.append(args)
    if args[:3] == ("git", "ls-remote", "--heads") and transcript.state.get("testpypi_sha"):
        return "test-head refs/heads/testpypi-" + transcript.state["version"]
    if args == ("git", "rev-parse", "origin/dev"):
        return "new-dev"
    if args == ("git", "rev-list", "--parents", "-n", "1", "test-head"):
        return "test-head old"
    if args[:2] == ("git", "diff"):
        return "pyproject.toml"
    if args[:2] == ("git", "show"):
        return _replacement_metadata(transcript.files, args)
    return ""


@pytest.fixture
def replacing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    root = _write_minimal_repo_files(tmp_path / "project")
    version, files, _fragments = preparation._metadata_files(root, "Old notes")
    for path, content in files.items():
        (root / path).write_text(content)
    state = {
        "task_id": "release",
        "task_digest": "digest",
        "pr": 12,
        "branch": "old-branch",
        "version": version,
        "candidate_sha": "old",
        "candidate_tree": "old-tree",
        "files": files,
        "stages": {"reviewed": "old"},
        "workspace": str(root),
    }
    (root / "docs/CHANGELOG.md").write_text(
        "## Unreleased\n\n- New dev notes\n\n" + files["docs/CHANGELOG.md"],
    )
    current = [state]
    calls = []
    monkeypatch.setattr(preparation, "state_path", lambda root: root / "atk-release.json")
    monkeypatch.setattr(preparation, "load", lambda root: current[0])
    original_save = preparation.save

    def save(root, value):
        original_save(root, value)
        current[0] = value

    monkeypatch.setattr(preparation, "save", save)
    monkeypatch.setattr(preparation.task_context, "directory", lambda root: root / "no-task")
    monkeypatch.setattr(preparation.sessions, "clean", lambda root: None)
    monkeypatch.setattr(preparation, "_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(candidate, "index_files", lambda *args: {})
    monkeypatch.setattr(candidate, "_runs", lambda *args: [])

    transcript = SimpleNamespace(state=state, calls=calls, files=files)

    def run(root, *args):
        return _replacement_source_git(transcript, *args)

    monkeypatch.setattr(preparation.sessions, "run", run)
    monkeypatch.setattr(
        preparation,
        "_tree",
        lambda root, ref: "old-tree" if ref == "old" else "new-tree",
    )

    def pr(root, value):
        merged = value["pr"] == 12 or value.get("merged")
        return {
            "number": value["pr"],
            "url": f"https://example/pr/{value['pr']}",
            "state": "MERGED" if merged else "OPEN",
            "mergedAt": "today" if merged else None,
            "headRefOid": value["candidate_sha"],
            "statusCheckRollup": [
                {"name": name, "conclusion": "SUCCESS"} for name in ("fast-checks", "agent-review")
            ],
        }

    monkeypatch.setattr(preparation, "_pr", pr)
    return root, state, current, calls


def _replacement_test_git(transcript, root, *args):
    if args[:3] == ("git", "ls-remote", "--heads"):
        return (
            "test-head refs/heads/testpypi-" + transcript.old["version"]
            if transcript.remote
            else ""
        )
    refs = {
        ("git", "rev-parse", "FETCH_HEAD"): "old",
        ("git", "rev-parse", "FETCH_HEAD^{tree}"): "old-tree",
        ("git", "rev-parse", "HEAD"): "test-head",
        ("git", "rev-parse", candidate._test_local_ref(transcript.old)): "test-head",
    }
    if args in refs:
        return refs[args]
    if args[:2] == ("git", "push"):
        if args[-1].startswith(":"):
            transcript.remote.clear()
        else:
            transcript.push_snapshots.append(json.loads(preparation.state_path(root).read_text()))
            transcript.remote.append("test-head")
            raise KeyboardInterrupt
    return transcript.original(root, *args)


def _new_replacement_generation(root, summary, *, predecessor, old, preparations):
    intent = predecessor["replacement_intent"]
    preparations.append(intent.copy())
    notes = replacement.notes(root, predecessor, intent["version"])
    version, files, fragments = preparation._metadata_files(
        root,
        summary,
        version=intent["version"],
        changelog=notes,
    )
    value = {
        "task_id": old["task_id"],
        "task_digest": old["task_digest"],
        "workspace": str(root),
        "source_sha": intent["source_sha"],
        "generation": intent["generation"],
        "predecessor_sha": predecessor["candidate_sha"],
        "version": version,
        "files": files,
        "fragments": fragments,
        "stages": {},
        "pr": None,
        "branch": "new-branch",
        "candidate_sha": "new" if intent["generation"] == 1 else "newer",
        "candidate_tree": "new-tree",
    }
    preparation.save(root, value)
    return value


def _assert_replacement_receipts(current, preparations, publications, receipts):
    assert len(preparations) == 1
    assert publications == ["new-branch"]
    assert not any(path.exists() for path in receipts)
    assert current[0]["stages"] == {}
    assert current[0]["generation"] == 1


def _assert_next_replacement(monkeypatch, root, current, preparations):
    # Another dev movement after this generation is merged starts one more PR.
    monkeypatch.setattr(
        preparation,
        "_tree",
        lambda root, ref: "third-tree" if ref == "origin/dev" else "new-tree",
    )
    assert preparation.prepare(root)["status"] == "pending"
    assert preparations[-1]["generation"] == 2
    assert len(preparations) == 2
    assert current[0]["predecessor_sha"] == "new"
    assert current[0]["stages"] == {}


def _start_replacement_ref(transcript, monkeypatch, branch_state):
    if branch_state == "legacy":
        transcript.remote.append("test-head")
        preparation.save(transcript.root, transcript.old)
    elif branch_state == "interrupted-push":
        monkeypatch.setattr(candidate.sessions, "load", lambda root: {"signing_key": "key"})
        monkeypatch.setattr(candidate.sessions, "save", lambda *args: None)
        monkeypatch.setattr(candidate.sessions, "commit_options", lambda root: [])
        with pytest.raises(KeyboardInterrupt):
            candidate._test_branch(transcript.root, transcript.old)
        assert len(transcript.push_snapshots) == 1
        assert transcript.push_snapshots[0]["testpypi_push_pending"]
        assert transcript.push_snapshots[0]["testpypi_sha"] == "test-head"
        # Process restart discards the mutated in-memory state.
        transcript.old = json.loads(preparation.state_path(transcript.root).read_text())
        transcript.current[0] = transcript.old
    return transcript.old


@pytest.mark.parametrize("branch_state", ["none", "legacy", "interrupted-push"])
def test_new_dev_prepares_one_reviewed_generation_and_invalidates_receipts(
    replacing,
    monkeypatch: pytest.MonkeyPatch,
    branch_state: str,
) -> None:
    root, old, current, _calls = replacing
    original = candidate.sessions.run
    remote = []
    push_snapshots = []

    transcript = SimpleNamespace(
        old=old,
        remote=remote,
        push_snapshots=push_snapshots,
        original=original,
        root=root,
        current=current,
    )

    def run(root, *args):
        return _replacement_test_git(transcript, root, *args)

    monkeypatch.setattr(candidate.sessions, "run", run)
    old = _start_replacement_ref(transcript, monkeypatch, branch_state)
    receipts = [
        root / path
        for path in (
            mcp_server.CHECK_STATE_FILE,
            mcp_server.PRECOMMIT_STAGE_STATE_FILE,
            mcp_server.RELEASE_CHECK_STATE_FILE,
        )
    ]
    for path in receipts:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old validation")
    preparations = []

    def new(root, summary, *, predecessor):
        return _new_replacement_generation(
            root,
            summary,
            predecessor=predecessor,
            old=old,
            preparations=preparations,
        )

    publications = []

    def publish(root, value):
        publications.append(value["branch"])
        value["pr"] = 13

    monkeypatch.setattr(preparation, "_new", new)
    monkeypatch.setattr(preparation, "_publish_preparation", publish)
    first = preparation.prepare(root)
    assert first["status"] == "pending"
    assert first["pr"] == 13
    assert first["version"] == old["version"]
    assert preparation.prepare(root) == first
    _assert_replacement_receipts(current, preparations, publications, receipts)
    archived = json.loads((root / "atk-release.json.old").read_text())
    assert archived["candidate_sha"] == "old"
    if branch_state != "none":
        assert archived["testpypi_sha"] == "test-head"
        assert "testpypi_push_pending" not in archived
        assert not remote
    assert current[0]["files"]["docs/CHANGELOG.md"].count("## " + old["version"]) == 1
    assert "- Old notes." in current[0]["files"]["docs/CHANGELOG.md"]
    assert "- New dev notes" in current[0]["files"]["docs/CHANGELOG.md"]
    with pytest.raises(RuntimeError, match="reviewed and merged"):
        preparation.verify(root, promotion=True)
    current[0]["merged"] = True
    assert preparation.prepare(root)["status"] == "prepared"
    assert preparation.verify(root, promotion=True)["stages"] == {"reviewed": "new"}
    _assert_next_replacement(monkeypatch, root, current, preparations)


def test_interrupted_replacement_intent_reuses_generation(
    replacing,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, current, _calls = replacing
    attempts = []

    def interrupted(root, summary, *, predecessor):
        attempts.append(predecessor["replacement_intent"].copy())
        raise KeyboardInterrupt

    monkeypatch.setattr(preparation, "_new", interrupted)
    for _retry in range(2):
        with pytest.raises(KeyboardInterrupt):
            preparation.prepare(root)
    assert attempts[0] == attempts[1]
    assert current[0]["replacement_pending"]
    assert attempts[0]["generation"] == 1
    assert attempts[0]["version"] == state["version"]


def _reservation_git(transcript, root, *args):
    if args[:3] == ("git", "ls-remote", "--heads") and transcript.reservation == "testpypi":
        return "test-head refs/heads/testpypi-" + transcript.old
    if args[:3] == ("git", "ls-remote", "--tags") and transcript.reservation == "remote-tag":
        return "old refs/tags/v" + transcript.old if "v" + transcript.old in args else ""
    if args[:3] == ("git", "tag", "--list") and transcript.reservation == "local-tag":
        return "v" + transcript.old if args[-1] == "v" + transcript.old else ""
    if args[:2] == ("gh", "api"):
        return "[]"
    return transcript.original(root, *args)


@pytest.mark.parametrize("published_hash", ["hash", "conflicting-hash"])
@pytest.mark.parametrize("reservation", ["testpypi", "pypi", "remote-tag", "local-tag"])
def test_reserved_candidate_uses_next_unreserved_version(
    replacing,
    monkeypatch: pytest.MonkeyPatch,
    reservation: str,
    published_hash: str,
) -> None:
    root, state, _current, calls = replacing
    old = state["version"]
    next_version = mcp_server._increment_version(old)
    final = mcp_server._increment_version(next_version)

    def index(stage, version):
        if version == old and stage == reservation:
            return {"package.whl": published_hash}
        if version == next_version:
            return {"collision.whl": "hash"}
        return {}

    original = candidate.sessions.run

    transcript = SimpleNamespace(reservation=reservation, old=old, original=original)

    def run(root, *args):
        return _reservation_git(transcript, root, *args)

    monkeypatch.setattr(candidate, "index_files", index)
    monkeypatch.setattr(candidate.sessions, "run", run)
    monkeypatch.setattr(
        candidate,
        "_runs",
        lambda root, ref, sha, stage: (
            [{"databaseId": 42, "status": "completed"}] if stage == reservation else []
        ),
    )
    monkeypatch.setattr(
        candidate,
        "_jobs",
        lambda *args: {
            "build distribution": "success",
            "publish to PyPI": "success",
            "publish to TestPyPI": "success",
        },
    )
    monkeypatch.setattr(candidate, "artifact_hashes", lambda *args: {"package.whl": "hash"})
    if reservation == "testpypi":
        state["testpypi_sha"] = "test-head"
    selected = []

    def new(root, summary, *, predecessor):
        selected.append(predecessor["replacement_intent"]["version"])
        value = {"candidate_sha": None, "version": selected[-1], "stages": {}}
        preparation.save(root, value)
        return value

    monkeypatch.setattr(preparation, "_new", new)
    replacement.replace(root, state, None)
    assert selected == [final]
    assert (root / "atk-release.json.old").exists()
    assert not any("push" in call for call in calls)


@pytest.mark.parametrize("failure", ["active", "uncertain", "saved-run", "index", "artifacts"])
def test_reconciliation_failure_preserves_candidate_and_receipts(
    replacing,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    root, state, current, _calls = replacing
    receipt = root / mcp_server.CHECK_STATE_FILE
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text("valid for old candidate")
    if failure == "active":
        monkeypatch.setattr(
            candidate,
            "_runs",
            lambda *args: [{"databaseId": 42, "status": "in_progress"}],
        )
    elif failure == "uncertain":
        clock = FakeClock(1000)
        monkeypatch.setattr(candidate.time, "time", clock)
        state["testpypi_sha"] = "test-head"
        state["testpypi_requested"] = "test-head"
    elif failure == "saved-run":
        state["pypi_run"] = 42
    elif failure == "index":

        def unavailable(*args):
            message = "index lookup failed"
            raise RuntimeError(message)

        monkeypatch.setattr(candidate, "index_files", unavailable)
    else:
        monkeypatch.setattr(candidate, "index_files", lambda *args: {"file": "hash"})
    with pytest.raises(RuntimeError, match=r"active|uncertain|unavailable|lookup failed|evidence"):
        replacement.replace(root, state, None)
    assert current[0]["candidate_sha"] == "old"
    assert "replacement_intent" not in state
    assert receipt.exists()
    assert not (root / "atk-release.json.old").exists()


def test_new_version_notes_preserve_previous_history_and_merge_new_entries(replacing) -> None:
    root, state, _current, _calls = replacing
    old = state["files"]["docs/CHANGELOG.md"]
    version = mcp_server._increment_version(state["version"])
    planned = replacement.notes(root, state, version)
    _version, files, _fragments = preparation._metadata_files(
        root,
        None,
        version=version,
        changelog=planned,
    )
    result = files["docs/CHANGELOG.md"]
    assert old[old.index("## " + state["version"]) :].strip() in result
    assert result.count("## " + state["version"]) == 1
    assert "- New dev notes" in result.split("## " + state["version"])[0]
    assert "- Old notes." in result.split("## " + state["version"])[0]


def test_new_workspace_uses_journaled_source_and_release_task(
    replacing,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root, state, current, calls = replacing
    work = _write_minimal_repo_files(tmp_path / "work", version=state["version"])
    (work / "docs/CHANGELOG.md").write_text(state["files"]["docs/CHANGELOG.md"])
    key = tmp_path / "signing-key"
    key.write_text("private")
    key.with_suffix(".pub").write_text("public")
    context = {"task_id": "release"}
    for directory in (root / "task", work / "task"):
        directory.mkdir()
        (directory / "active.json").write_text(json.dumps(context))
    monkeypatch.setattr(preparation.task_context, "directory", lambda root: root / "task")
    monkeypatch.setattr(preparation.task_context, "read", lambda root: context)
    monkeypatch.setattr(
        preparation.task_context,
        "protocol",
        lambda: SimpleNamespace(
            public_context=lambda value: value,
            digest=lambda value: "digest",
        ),
    )
    monkeypatch.setattr(preparation.sessions, "create", lambda *args: work)
    monkeypatch.setattr(preparation.sessions, "load", lambda root: {"signing_key": str(key)})
    monkeypatch.setattr(preparation.sessions, "save", lambda *args: None)
    state["replacement_intent"] = {
        "source_sha": "journaled-dev",
        "version": state["version"],
        "generation": 2,
    }
    new = preparation._new(root, None, predecessor=state)
    assert current[0] == new
    assert new["source_sha"] == "journaled-dev"
    assert new["generation"] == 2
    assert new["task_id"] == state["task_id"]
    assert new["task_digest"] == state["task_digest"]
    assert new["branch"].endswith("-g2")
    assert new["predecessor_sha"] == "old"
    assert new["reset_receipts"]
    assert new["stages"] == {}
    assert ("git", "fetch", "origin", "journaled-dev") in calls
    assert ("git", "switch", "--detach", "journaled-dev") in calls


def test_empty_unreleased_section_is_accepted(replacing) -> None:
    root, state, _current, _calls = replacing
    state["files"]["docs/CHANGELOG.md"] = "## Unreleased\n\n" + state["files"]["docs/CHANGELOG.md"]
    expected = {path: content.strip() + "\n" for path, content in state["files"].items()}
    assert preparation._reviewed_metadata(root, state, "old") == expected


def test_missing_release_workflow_blocks_reserved_tag_replacement(
    replacing,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, _current, calls = replacing
    original = candidate.sessions.run

    def run(root, *args):
        if args[:3] == ("git", "ls-remote", "--tags"):
            return "old refs/tags/v" + state["version"]
        if args[:2] == ("gh", "api"):
            return json.dumps([[{"tag_name": "v" + state["version"]}]])
        return original(root, *args)

    monkeypatch.setattr(candidate.sessions, "run", run)
    with pytest.raises(RuntimeError, match="uncertain"):
        replacement.replace(root, state, None)
    assert not any("push" in call for call in calls)
    assert "replacement_intent" not in state


def test_expired_uncertain_dispatch_is_rechecked_before_replacement(
    replacing,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, _current, _calls = replacing
    clock = FakeClock(1000)
    monkeypatch.setattr(candidate.time, "time", clock)
    state["testpypi_sha"] = "test-head"
    state["testpypi_requested"] = "test-head"
    with pytest.raises(RuntimeError, match="uncertain"):
        candidate.reconcile_replacement(root, state, allow_reserved=True)
    clock.sleep(candidate.DISPATCH_RECONCILIATION_SECONDS)
    assert not candidate.reconcile_replacement(root, state, allow_reserved=True)


def test_replacement_activation_recovers_receipt_invalidation(
    replacing,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, _current, _calls = replacing
    receipt = root / mcp_server.CHECK_STATE_FILE
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text("old validation")
    state.update(reset_receipts=True, pr=13)
    monkeypatch.setattr(
        preparation,
        "_publish_preparation",
        lambda *args: pytest.fail("duplicate PR"),
    )
    assert preparation.prepare(root)["status"] == "pending"
    assert not receipt.exists()
    assert "reset_receipts" not in state


def test_interruption_after_replacement_journal_resumes_one_workspace(
    replacing,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, old, current, _calls = replacing
    receipt = root / mcp_server.CHECK_STATE_FILE
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text("old validation")
    created = []

    def interrupted(root, summary, *, predecessor):
        intent = predecessor["replacement_intent"]
        created.append(intent.copy())
        preparation.save(
            root,
            {
                **old,
                "generation": intent["generation"],
                "source_sha": intent["source_sha"],
                "predecessor_sha": predecessor["candidate_sha"],
                "branch": "replacement-branch",
                "candidate_sha": None,
                "candidate_tree": None,
                "pr": None,
                "stages": {},
                "reset_receipts": True,
                "replacement_pending": False,
            },
        )
        raise KeyboardInterrupt

    monkeypatch.setattr(preparation, "_new", interrupted)
    with pytest.raises(KeyboardInterrupt):
        preparation.prepare(root)
    assert current[0]["reset_receipts"]
    assert receipt.exists()
    publications = []

    def publish(root, value):
        publications.append(value["branch"])
        value.update(pr=13, candidate_sha="new", candidate_tree="new-tree")

    monkeypatch.setattr(preparation, "_publish_preparation", publish)
    result = preparation.prepare(root)
    assert result["status"] == "pending"
    assert result["pr"] == 13
    assert preparation.prepare(root) == result
    assert len(created) == 1
    assert publications == ["replacement-branch"]
    assert current[0]["generation"] == 1
    assert current[0]["version"] == old["version"]
    assert not receipt.exists()
    assert "reset_receipts" not in current[0]
