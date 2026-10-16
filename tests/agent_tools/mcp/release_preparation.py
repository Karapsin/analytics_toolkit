from __future__ import annotations

from types import SimpleNamespace

import pytest
from release_routines.lib import release_candidate as candidate

from agent_tools import release_preparation as preparation
from agent_tools import release_replacement as replacement
from tests.agent_tools._support.mcp import (
    Path,
    _write_minimal_repo_files,
    _write_unreleased_changelog,
    mcp_server,
)


@pytest.fixture(autouse=True)
def private_release_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    # These unit tests simulate release Git/task storage without a real checkout.
    monkeypatch.setattr(preparation, "state_path", lambda root: root / "atk-release.json")
    monkeypatch.setattr(preparation.task_context, "directory", lambda root: root / "no-task")


@pytest.mark.parametrize("summary", [None, "", "  "])
def test_empty_release_needs_summary(tmp_path: Path, summary: str | None) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    originals = {path: (root / path).read_text() for path in preparation.METADATA}
    with pytest.raises(ValueError, match="supply --summary"):
        preparation._metadata_files(root, summary)
    assert {path: (root / path).read_text() for path in originals} == originals


def test_summary_and_fragments_are_planned_without_writes(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project", version="1.3.19.19")
    version, files, consumed = preparation._metadata_files(root, "Release README metadata")
    assert version == "1.4.0.0"
    assert consumed == []
    assert "Release README metadata." in files["docs/CHANGELOG.md"]
    assert mcp_server._package_version(root) == "1.3.19.19"
    fragments = root / "agent_tools/changelog"
    fragments.mkdir(parents=True)
    (fragments / "one.md").write_text("- Pending feature\n")
    _write_unreleased_changelog(root, ["Wrapped\n  continuation"])
    version, files, consumed = preparation._metadata_files(root, None)
    assert version == "1.4.0.0"
    assert consumed == ["agent_tools/changelog/one.md"]
    assert "- Wrapped\n  continuation" in files["docs/CHANGELOG.md"]
    assert "- Pending feature" in files["docs/CHANGELOG.md"]
    assert (fragments / "one.md").exists()


@pytest.mark.parametrize("bad_marker", ["missing", "duplicate"])
def test_release_metadata_validation_is_atomic(tmp_path: Path, bad_marker: str) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    readme = root / "README.md"
    readme.write_text("# README\n" if bad_marker == "missing" else readme.read_text() * 2)
    originals = {path: (root / path).read_text() for path in preparation.METADATA}
    with pytest.raises(ValueError, match="README version"):
        preparation._metadata_files(root, "Release metadata")
    result = mcp_server.version_bump(
        "Release metadata", change_type="release", dry_run=True, root=str(root)
    )
    assert not result["ok"]
    assert {path: (root / path).read_text() for path in originals} == originals


def test_summary_only_force_release_dry_run_does_not_write(tmp_path: Path) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    originals = {path: (root / path).read_text() for path in preparation.METADATA}
    planned = mcp_server.version_bump("README-only release", "release", True, True, str(root))
    assert planned["ok"]
    assert planned["result"]["planned_version"] == "1.3.9.14"
    assert {path: (root / path).read_text() for path in originals} == originals


@pytest.mark.parametrize("action", ["merge-dev", "publish"])
def test_pending_pr_cannot_promote_or_publish(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    monkeypatch.setattr(
        mcp_server,
        "_prepare_release",
        lambda *args: {
            "status": "pending",
            "pr": 12,
            "url": "https://example/pr/12",
            "version": "1.2.3.4",
        },
    )
    monkeypatch.setattr(
        mcp_server,
        "_merge_dev_for_release",
        lambda *args: pytest.fail("promoted pending PR"),
    )
    monkeypatch.setattr(
        mcp_server,
        "_run_command",
        lambda *args: pytest.fail("uploaded pending PR"),
    )
    result = mcp_server.release_workflow(action)
    assert result["result"]["status"] == "pending"
    assert result["result"]["pr"] == 12
    assert "resume" in result["next_actions"][0]


def test_missing_preparation_blocks_status_without_preparing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    monkeypatch.setattr(preparation, "load", lambda root: {})
    monkeypatch.setattr(
        mcp_server,
        "_prepare_release",
        lambda *args: pytest.fail("status prepared metadata"),
    )
    monkeypatch.setattr(
        mcp_server,
        "repo_health",
        lambda **kwargs: {"branch": "main", "dirty": False},
    )
    for name in ("_remote_main_status", "_remote_dev_status"):
        monkeypatch.setattr(
            mcp_server,
            name,
            lambda *args, **kwargs: {
                "result": {},
                "command_results": [],
                "blockers": [],
            },
        )
    monkeypatch.setattr(mcp_server, "_verify_precommit_success", lambda *args: {"ok": True})
    result = mcp_server.release_workflow("status", str(root))
    assert not result["ok"]
    assert any(blocker["phase"] == "preparation" for blocker in result["blockers"])


def test_reentering_pending_preparation_reuses_pr_and_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = {
        "pr": 12,
        "branch": "codex/release-task-source",
        "version": "1.2.3.4",
        "candidate_sha": "candidate",
        "candidate_tree": "tree",
        "stages": {},
    }
    monkeypatch.setattr(preparation, "load", lambda root: state)
    monkeypatch.setattr(preparation.sessions, "clean", lambda root: None)
    monkeypatch.setattr(preparation.task_context, "directory", lambda root: root / "no-task")
    monkeypatch.setattr(preparation.sessions, "run", lambda *args: "")
    monkeypatch.setattr(preparation, "_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        preparation,
        "_pr",
        lambda *args: {
            "number": 12,
            "url": "https://example/pr/12",
            "state": "OPEN",
            "mergedAt": None,
        },
    )
    monkeypatch.setattr(preparation, "_new", lambda *args: pytest.fail("duplicate preparation"))
    monkeypatch.setattr(
        preparation,
        "_publish_preparation",
        lambda *args: pytest.fail("duplicate PR"),
    )
    first = preparation.prepare(tmp_path)
    second = preparation.prepare(tmp_path)
    assert first == second
    assert second["version"] == "1.2.3.4"


def test_closed_pr_blocks_resume(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(preparation, "load", lambda root: {"pr": 12})
    monkeypatch.setattr(preparation.sessions, "clean", lambda root: None)
    monkeypatch.setattr(preparation.sessions, "run", lambda *args: "")
    monkeypatch.setattr(preparation, "_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(preparation, "_pr", lambda *args: {"state": "CLOSED", "mergedAt": None})
    with pytest.raises(RuntimeError, match="closed without merging"):
        preparation.prepare(tmp_path)


@pytest.mark.parametrize("tree", ["changed-dev", "changed-candidate"])
def test_changed_content_invalidates_prepared_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tree: str,
) -> None:
    monkeypatch.setattr(
        preparation,
        "load",
        lambda root: {
            "candidate_sha": "candidate",
            "candidate_tree": "tree",
            "pr": 12,
        },
    )
    monkeypatch.setattr(preparation.sessions, "clean", lambda root: None)
    monkeypatch.setattr(preparation.sessions, "run", lambda *args: "")
    monkeypatch.setattr(preparation, "_reviewed", lambda *args: {})
    monkeypatch.setattr(preparation, "_tree", lambda root, ref: tree)
    with pytest.raises(RuntimeError, match="differs"):
        preparation.verify(tmp_path)


@pytest.mark.parametrize("check", ["fast-checks", "agent-review"])
def test_merged_pr_needs_successful_exact_candidate_checks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    check: str,
) -> None:
    state = {"candidate_sha": "candidate"}
    monkeypatch.setattr(preparation, "_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        preparation,
        "_pr",
        lambda *args: {
            "mergedAt": "today",
            "headRefOid": "candidate",
            "statusCheckRollup": [
                {"name": name, "conclusion": "FAILURE" if name == check else "SUCCESS"}
                for name in ("fast-checks", "agent-review")
            ],
        },
    )
    with pytest.raises(RuntimeError, match=check):
        preparation._reviewed(tmp_path, state)


def test_preparation_commit_does_not_reopen_unreleased(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    monkeypatch.setattr(
        mcp_server.sessions,
        "load",
        lambda root: {
            "phase": "feature",
            "release_preparation": True,
            "id": "release-task",
        },
    )
    monkeypatch.setattr(mcp_server, "_changed_repo_paths", lambda root: set(preparation.METADATA))
    requirement = mcp_server._version_bump_requirement(root, list(preparation.METADATA))
    assert requirement["required_paths"] == sorted(preparation.METADATA)
    assert requirement["missing"] == []


def test_summary_cli() -> None:
    parser = mcp_server._build_cli_parser()
    args = parser.parse_args(
        ["release-workflow", "--action", "merge-dev", "--summary", "README release"]
    )
    assert args.summary == "README release"


def test_interrupted_signed_commit_recovers_without_incrementing_again(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    work = _write_minimal_repo_files(tmp_path / "workspace")
    version, files, fragments = preparation._metadata_files(work, "Candidate notes")
    state = {
        "workspace": str(work),
        "source_sha": "source",
        "candidate_sha": None,
        "branch": "codex/release",
        "version": version,
        "files": files,
        "fragments": fragments,
        "task_id": "release",
        "task_digest": "digest",
    }
    head = ["source"]
    commits = []
    signatures = []

    def run(root, *args):
        if args == ("git", "rev-parse", "HEAD"):
            return head[0]
        if args == ("git", "branch", "--show-current"):
            return "codex/release"
        if args[:2] == ("git", "commit"):
            commits.append(args)
            head[0] = "signed-candidate"
            message = "interrupted after commit"
            raise RuntimeError(message)
        if args[:2] == ("git", "show"):
            return files[args[2].split(":", 1)[1]].strip()
        return ""

    monkeypatch.setattr(preparation.sessions, "run", run)
    monkeypatch.setattr(preparation.sessions, "commit_options", lambda root: [])
    monkeypatch.setattr(
        preparation.sessions,
        "verify_signature",
        lambda root, sha: signatures.append(sha),
    )
    monkeypatch.setattr(preparation.sessions, "load", lambda root: {"phase": "feature"})
    monkeypatch.setattr(preparation.sessions, "push", lambda root: {"pr": 12})
    monkeypatch.setattr(
        preparation,
        "_published_preparation",
        lambda *args: {"number": 12, "url": "https://example/pr/12"},
    )
    monkeypatch.setattr(preparation, "_recover_preparation_handoff", lambda *args: None)
    monkeypatch.setattr(
        preparation.task_context,
        "commit_trailers",
        lambda root: "\n\nUser-Task: release",
    )
    monkeypatch.setattr(preparation.task_context, "read", lambda root: {})
    monkeypatch.setattr(
        preparation.task_context,
        "protocol",
        lambda: SimpleNamespace(
            public_context=lambda value: value,
            verify_commit=lambda *args: None,
        ),
    )
    monkeypatch.setattr(preparation, "save", lambda *args: None)
    monkeypatch.setattr(preparation, "_tree", lambda *args: "tree")
    monkeypatch.setattr(
        preparation,
        "_pr",
        lambda *args: {"number": 12, "url": "https://example/pr/12"},
    )
    monkeypatch.setattr(preparation, "_context", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="interrupted"):
        preparation._publish_preparation(tmp_path, state)
    preparation._publish_preparation(tmp_path, state)
    assert len(commits) == 1
    assert signatures == ["signed-candidate"]
    assert state["candidate_sha"] == "signed-candidate"
    assert state["pr"] == 12
    assert mcp_server._package_version(work) == version


@pytest.mark.parametrize("field", ["task_id", "task_digest", "branch", "base"])
def test_preparation_pr_is_bound_to_release_task_and_branches(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    state = {"task_id": "release", "task_digest": "digest", "branch": "codex/release"}
    pr = {"body": "attachment", "headRefName": "codex/release", "baseRefName": "dev"}
    proto = SimpleNamespace(
        parse=lambda body: {"task_id": "release"}, digest=lambda value: "digest"
    )
    monkeypatch.setattr(preparation.task_context, "protocol", lambda: proto)
    if field == "base":
        pr["baseRefName"] = "main"
    elif field == "branch":
        pr["headRefName"] = "other"
    else:
        state[field] = "other"
    with pytest.raises(RuntimeError, match="signed release task"):
        preparation._context(state, pr)


@pytest.fixture
def corrected_preparation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    root = _write_minimal_repo_files(tmp_path / "project")
    version, files, _fragments = preparation._metadata_files(root, "Preserved release notes")
    state = {
        "pr": 12,
        "branch": "codex/release",
        "task_id": "release",
        "task_digest": "digest",
        "version": version,
        "candidate_sha": "old",
        "candidate_tree": "old-tree",
        "files": files,
        "stages": {"reviewed": "old"},
    }
    pr = {
        "number": 12,
        "url": "https://example/pr/12",
        "state": "MERGED",
        "mergedAt": "today",
        "headRefOid": "corrected",
        "statusCheckRollup": [
            {"name": name, "conclusion": "SUCCESS"} for name in ("fast-checks", "agent-review")
        ],
    }
    calls = []

    def run(root, *args):
        calls.append(args)
        if args[:2] == ("git", "show"):
            return files[args[2].split(":", 1)[1]].strip()
        return ""

    monkeypatch.setattr(preparation, "load", lambda root: state)
    monkeypatch.setattr(preparation, "save", lambda *args: None)
    monkeypatch.setattr(preparation.sessions, "clean", lambda root: None)
    monkeypatch.setattr(preparation.sessions, "run", run)
    monkeypatch.setattr(preparation, "_tree", lambda *args: "corrected-tree")
    monkeypatch.setattr(preparation, "_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(preparation, "_pr", lambda *args: pr)
    monkeypatch.setattr(
        preparation,
        "_new",
        lambda *args: pytest.fail("incremented corrected release"),
    )
    monkeypatch.setattr(
        preparation,
        "_publish_preparation",
        lambda *args: pytest.fail("created another PR"),
    )
    monkeypatch.setattr(candidate, "index_files", lambda *args: {})
    monkeypatch.setattr(candidate, "_runs", lambda *args: [])
    return root, state, pr, files, calls


def test_corrected_merged_preparation_preserves_version_notes_and_pr(
    corrected_preparation,
) -> None:
    root, state, _pr, files, calls = corrected_preparation
    old_version = state["version"]
    result = preparation.prepare(root)
    assert result["status"] == "prepared"
    assert state["candidate_sha"] == "corrected"
    assert state["candidate_tree"] == "corrected-tree"
    assert state["version"] == old_version
    assert state["pr"] == 12
    assert "Preserved release notes" in state["files"]["docs/CHANGELOG.md"]
    assert not mcp_server._unreleased_changelog_bullets(state["files"]["docs/CHANGELOG.md"])
    assert state["stages"] == {"reviewed": "corrected"}
    assert (root / "atk-release.json.old").exists()
    assert ("git", "fetch", "origin", "corrected") in calls
    assert preparation.prepare(root) == result
    assert files["docs/CHANGELOG.md"].strip() == state["files"]["docs/CHANGELOG.md"].strip()
    assert not any("commit" in call for call in calls)


@pytest.mark.parametrize("check", ["fast-checks", "agent-review"])
def test_corrected_head_needs_its_own_successful_checks(
    corrected_preparation,
    check: str,
) -> None:
    root, state, pr, _files, _calls = corrected_preparation
    for item in pr["statusCheckRollup"]:
        if item["name"] == check:
            item["conclusion"] = "FAILURE"
    with pytest.raises(RuntimeError, match=check):
        preparation.prepare(root)
    assert state["candidate_sha"] == "old"
    assert not (root / "atk-release.json.old").exists()


@pytest.mark.parametrize(
    "problem", ["version", "readme", "notes", "unreleased", "fragments", "dev", "task"]
)
def test_invalid_corrected_candidate_is_not_adopted(
    corrected_preparation,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    root, state, _pr, files, _calls = corrected_preparation
    if problem in {"version", "readme"}:
        path = "pyproject.toml" if problem == "version" else "README.md"
        files[path] = files[path].replace(state["version"], "9.9.9.9")
    elif problem == "notes":
        files["docs/CHANGELOG.md"] = "# Changelog\n\n## Unreleased\n"
    elif problem == "unreleased":
        files["docs/CHANGELOG.md"] = (
            "## Unreleased\n\n- Pending change\n\n" + files["docs/CHANGELOG.md"]
        )
    elif problem == "dev":
        monkeypatch.setattr(
            preparation,
            "_tree",
            lambda root, sha: "unrelated" if sha == "origin/dev" else "corrected-tree",
        )

        def request_replacement(*args):
            message = "additional content replacement requested"
            raise RuntimeError(message)

        monkeypatch.setattr(replacement, "replace", request_replacement)
    elif problem == "task":

        def invalid(*args, **kwargs):
            message = "invalid task binding"
            raise RuntimeError(message)

        monkeypatch.setattr(preparation, "_context", invalid)
    else:
        original = preparation.sessions.run
        monkeypatch.setattr(
            preparation.sessions,
            "run",
            lambda root, *args: (
                "agent_tools/changelog/pending.md"
                if args[:2] == ("git", "ls-tree")
                else original(root, *args)
            ),
        )
    with pytest.raises(
        RuntimeError, match=r"version|notes|unreleased|additional content|fragments|task binding"
    ):
        preparation.prepare(root)
    assert state["candidate_sha"] == "old"


def test_adoption_invalidates_old_validation_and_artifact_bindings(
    corrected_preparation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, _pr, _files, _calls = corrected_preparation
    state.update(
        testpypi_run=42,
        testpypi_sha="test-head",
        testpypi_dispatch={"old": "record"},
        pypi_run=43,
        pypi_artifacts={"file": "hash"},
    )
    state["stages"].update(testpypi_verified="old", pypi_verified="old")
    # Publication reconciliation has its own fail-closed tests.
    monkeypatch.setattr(candidate, "reconcile_replacement", lambda *args: None)
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
    preparation.prepare(root)
    assert all(not path.exists() for path in receipts)
    assert not any(key.startswith(("testpypi_", "pypi_")) for key in state)
    assert state["stages"] == {"reviewed": "corrected"}


def test_published_old_candidate_blocks_adoption(
    corrected_preparation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, _pr, _files, _calls = corrected_preparation
    monkeypatch.setattr(candidate, "index_files", lambda *args: {"package.whl": "old"})
    with pytest.raises(RuntimeError, match="new reviewed version"):
        preparation.prepare(root)
    assert state["candidate_sha"] == "old"
    assert not (root / "atk-release.json.old").exists()
