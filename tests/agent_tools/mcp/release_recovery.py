from __future__ import annotations

import json
import urllib.error
from types import SimpleNamespace

import pytest
from release_routines.lib import release_candidate as candidate

from tests.agent_tools._support.mcp import Path, _write_minimal_repo_files


@pytest.mark.parametrize("status", [401, 500])
def test_index_http_errors_block_upload(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    def unavailable(*args, **kwargs):
        url = "https://index"
        raise urllib.error.HTTPError(url, status, "failure", {}, None)

    monkeypatch.setattr(candidate.urllib.request, "urlopen", unavailable)
    with pytest.raises(RuntimeError, match="lookup failed"):
        candidate.index_files("pypi", "1.2.3.4")


def test_index_network_errors_and_malformed_metadata_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        candidate.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    with pytest.raises(RuntimeError, match="lookup failed"):
        candidate.index_files("pypi", "1.2.3.4")


def test_changed_published_artifact_requires_new_version() -> None:
    with pytest.raises(RuntimeError, match="new version"):
        candidate.compare_artifacts({"package.whl": "different"}, {"package.whl": "verified"})
    assert not candidate.compare_artifacts(
        {"package.whl": "same"},
        {
            "package.whl": "same",
            "package.tar.gz": "sdist",
        },
    )
    assert candidate.compare_artifacts({"package.whl": "same"}, {"package.whl": "same"})


@pytest.fixture
def publication(monkeypatch: pytest.MonkeyPatch):
    state = {
        "version": "1.2.3.4",
        "candidate_sha": "candidate",
        "candidate_tree": "tree",
        "stages": {},
        "pypi_run": 42,
        "testpypi_run": 42,
    }
    calls = []
    monkeypatch.setattr(
        candidate.release_preparation,
        "state_path",
        lambda root: root / "atk-release.json",
    )
    monkeypatch.setattr(candidate, "guard", lambda *args, **kwargs: state)
    monkeypatch.setattr(candidate.release_preparation, "save", lambda *args: None)
    monkeypatch.setattr(
        candidate.release_preparation.task_context,
        "directory",
        lambda root: root / "no-task",
    )
    monkeypatch.setattr(candidate, "_test_branch", lambda *args: "test-head")
    monkeypatch.setattr(candidate, "_production_ref", lambda *args: "v1.2.3.4")
    monkeypatch.setattr(
        candidate,
        "_runs",
        lambda *args: [
            {
                "databaseId": 42,
                "headSha": "candidate",
                "status": "completed",
                "conclusion": "failure",
            }
        ],
    )
    monkeypatch.setattr(
        candidate,
        "_jobs",
        lambda *args: {
            "build distribution": "success",
            "publish to TestPyPI": "skipped",
            "publish to PyPI": "failure",
        },
    )
    monkeypatch.setattr(
        candidate,
        "artifact_hashes",
        lambda *args: {
            "package.whl": "wheel",
            "package.tar.gz": "sdist",
        },
    )
    monkeypatch.setattr(candidate, "index_files", lambda *args: {})

    def run(root, *args):
        calls.append(args)
        return "candidate" if args[:2] == ("git", "rev-list") else ""

    monkeypatch.setattr(candidate.sessions, "run", run)
    return state, calls


@pytest.mark.parametrize("stage", ["pypi", "testpypi"])
def test_failed_upload_reuses_successful_build_and_only_reruns_failed_jobs(
    publication,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
) -> None:
    with pytest.raises(RuntimeError, match="requested again"):
        candidate.prepare_stage(tmp_path, stage)
    state, calls = publication
    assert ("gh", "run", "rerun", "42", "--failed") in calls
    assert not any("workflow" in call or "release" in call for call in calls)
    assert state[stage + "_run"] == 42
    assert state["stages"] == {}


def test_partial_upload_requires_matching_retained_artifacts(
    publication,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(candidate, "index_files", lambda *args: {"package.whl": "changed"})
    with pytest.raises(RuntimeError, match="new version"):
        candidate.prepare_stage(tmp_path, "pypi")
    assert not any("rerun" in call for call in publication[1])


def test_successful_upload_with_failed_job_resumes_verification(
    publication,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        candidate,
        "index_files",
        lambda *args: {
            "package.whl": "wheel",
            "package.tar.gz": "sdist",
        },
    )
    assert candidate.prepare_stage(tmp_path, "pypi") == 42
    assert not any("rerun" in call for call in publication[1])
    assert publication[0]["stages"] == {}  # Fresh-environment verification is still required.
    candidate.verified(tmp_path, "pypi")
    assert publication[0]["stages"]["pypi_verified"] == "candidate"


def test_verification_failure_does_not_mark_stage_complete(
    publication,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="Cannot record"):
        candidate.verified(tmp_path, "pypi")
    assert publication[0]["stages"] == {}


def test_completed_test_stage_survives_successful_branch_cleanup(
    publication,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state, _calls = publication
    state["stages"]["testpypi_verified"] = "candidate"
    monkeypatch.setattr(
        candidate,
        "index_files",
        lambda *args: {
            "package.whl": "wheel",
            "package.tar.gz": "sdist",
        },
    )
    monkeypatch.setattr(
        candidate,
        "_test_branch",
        lambda *args: pytest.fail("recreated deleted branch"),
    )
    assert candidate.prepare_stage(tmp_path, "testpypi") == 42


def test_pending_workflow_never_starts_another_upload(
    publication,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        candidate,
        "_runs",
        lambda *args: [{"databaseId": 42, "status": "in_progress", "conclusion": None}],
    )
    with pytest.raises(RuntimeError, match="pending"):
        candidate.prepare_stage(tmp_path, "pypi")
    assert not any("rerun" in call for call in publication[1])


def test_unavailable_saved_workflow_blocks_rebuild(
    publication,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(candidate, "_runs", lambda *args: [])
    with pytest.raises(RuntimeError, match="unavailable"):
        candidate.prepare_stage(tmp_path, "pypi")
    assert not any("create" in call or "rerun" in call for call in publication[1])


def test_existing_production_tag_mismatch_is_not_moved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = []

    def run(root, *args):
        calls.append(args)
        if args[:2] == ("git", "ls-remote"):
            return "other refs/tags/v1.2.3.4"
        return "candidate" if args[:2] == ("git", "rev-parse") else ""

    monkeypatch.setattr(candidate.sessions, "run", run)
    with pytest.raises(RuntimeError, match="different content"):
        candidate._production_ref(tmp_path, {"version": "1.2.3.4", "candidate_tree": "tree"})
    assert not any("push" in call or "--force" in call for call in calls)


def test_existing_test_branch_mismatch_is_not_recreated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = []

    def run(root, *args):
        calls.append(args)
        if args[:2] == ("git", "ls-remote"):
            return "test-head refs/heads/testpypi-1.2.3.4"
        return "different-parent" if args[:2] == ("git", "rev-parse") else ""

    monkeypatch.setattr(candidate.sessions, "run", run)
    with pytest.raises(RuntimeError, match="different candidate"):
        candidate._test_branch(tmp_path, {"version": "1.2.3.4", "candidate_sha": "candidate"})
    assert not any("push" in call or "switch" in call for call in calls)


def test_direct_upload_requires_preparation_and_exact_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    monkeypatch.setattr(
        candidate.release_preparation,
        "verify",
        lambda root: (_ for _ in ()).throw(RuntimeError("missing reviewed PR")),
    )
    with pytest.raises(RuntimeError, match="missing reviewed PR"):
        candidate.guard(root)
    monkeypatch.setattr(
        candidate.release_preparation,
        "verify",
        lambda root: {
            "candidate_sha": "candidate",
            "stages": {},
        },
    )
    mcp = SimpleNamespace(
        _verify_precommit_success=lambda root: {"ok": True},
        _verify_release_check_success=lambda root: {
            "ok": False,
            "message": "stale integration receipt",
        },
    )
    monkeypatch.setattr(candidate.release_preparation, "mcp_runtime", lambda: mcp)
    with pytest.raises(RuntimeError, match="stale integration receipt"):
        candidate.guard(root)
    mcp._verify_release_check_success = lambda root: {"ok": True}
    with pytest.raises(RuntimeError, match="verified TestPyPI"):
        candidate.guard(root, production=True)


def test_workflow_upload_guard_checks_existing_file_hashes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _write_minimal_repo_files(tmp_path / "project")
    dist = root / "dist"
    dist.mkdir()
    (dist / "package.whl").write_bytes(b"verified wheel")
    (dist / "package.tar.gz").write_bytes(b"verified sdist")
    hashes = candidate.distribution_hashes(dist)
    monkeypatch.setattr(candidate, "index_files", lambda *args: hashes)
    candidate.check_upload_files(root, "pypi")
    monkeypatch.setattr(candidate, "index_files", lambda *args: {"package.whl": "changed"})
    with pytest.raises(RuntimeError, match="new version"):
        candidate.check_upload_files(root, "pypi")


def test_failed_only_rerun_keeps_original_build_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pages = [
        {
            "jobs": [
                {"id": 1, "name": "build distribution", "conclusion": "success"},
                {"id": 2, "name": "publish to PyPI", "conclusion": "failure"},
            ]
        },
        {
            "jobs": [
                {"id": 3, "name": "publish to PyPI", "conclusion": "success"},
            ]
        },
    ]
    requests = []

    def run(root, *args):
        requests.append(args)
        return json.dumps(pages)

    monkeypatch.setattr(candidate.sessions, "run", run)
    jobs = candidate._jobs(tmp_path, 42)
    assert jobs["build distribution"] == "success"
    assert jobs["publish to PyPI"] == "success"
    assert "filter=all" in requests[0][2]


def test_opposite_index_job_cannot_publish(
    publication,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        candidate,
        "index_files",
        lambda *args: {
            "package.whl": "wheel",
            "package.tar.gz": "sdist",
        },
    )
    monkeypatch.setattr(
        candidate,
        "_jobs",
        lambda *args: {
            "build distribution": "success",
            "publish to TestPyPI": "success",
            "publish to PyPI": "success",
        },
    )
    with pytest.raises(RuntimeError, match="opposite-index"):
        candidate.prepare_stage(tmp_path, "pypi")


@pytest.fixture
def dispatch(publication, monkeypatch: pytest.MonkeyPatch):
    state, calls = publication
    state.pop("testpypi_run")
    monkeypatch.setattr(candidate, "_runs", lambda *args: [])
    clock = [1000.0]
    monkeypatch.setattr(candidate.time, "time", lambda: clock[0])
    return state, calls, clock


def test_interruption_before_dispatch_retries_same_candidate_after_deadline(
    dispatch,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state, calls, clock = dispatch

    def save(root, value):
        if value.get("testpypi_dispatch"):
            raise KeyboardInterrupt

    monkeypatch.setattr(candidate.release_preparation, "save", save)
    with pytest.raises(KeyboardInterrupt):
        candidate.prepare_stage(tmp_path, "testpypi")
    assert not any("workflow" in call for call in calls)
    deadline = state["testpypi_dispatch"]["deadline"]
    monkeypatch.setattr(candidate.release_preparation, "save", lambda *args: None)
    with pytest.raises(RuntimeError, match="uncertain"):
        candidate.prepare_stage(tmp_path, "testpypi")
    assert state["testpypi_dispatch"]["deadline"] == deadline
    clock[0] = deadline
    with pytest.raises(RuntimeError, match="workflow requested"):
        candidate.prepare_stage(tmp_path, "testpypi")
    assert sum("workflow" in call for call in calls) == 1
    assert state["version"] == "1.2.3.4"
    assert state["candidate_sha"] == "candidate"
    assert state["testpypi_sha"] == "test-head"


@pytest.mark.parametrize("failure", ["HTTP 422: rejected", "connection timed out"])
def test_failed_dispatch_recovery_preserves_candidate(
    dispatch,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    state, calls, clock = dispatch

    def fail(root, *args):
        calls.append(args)
        if "workflow" in args:
            raise RuntimeError(failure)
        return ""

    monkeypatch.setattr(candidate.sessions, "run", fail)
    with pytest.raises(RuntimeError, match=failure):
        candidate.prepare_stage(tmp_path, "testpypi")
    record = state["testpypi_dispatch"]
    assert record["outcome"] == ("failed" if failure.startswith("HTTP") else "uncertain")
    monkeypatch.setattr(candidate.sessions, "run", lambda root, *args: calls.append(args) or "")
    if record["outcome"] == "uncertain":
        with pytest.raises(RuntimeError, match="uncertain"):
            candidate.prepare_stage(tmp_path, "testpypi")
        clock[0] = record["deadline"]
    with pytest.raises(RuntimeError, match="workflow requested"):
        candidate.prepare_stage(tmp_path, "testpypi")
    assert sum("workflow" in call for call in calls) == 2
    assert state["version"] == "1.2.3.4"


def test_legacy_marker_starts_one_window_and_adopts_appearing_run(
    dispatch,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state, calls, clock = dispatch
    state["testpypi_requested"] = "test-head"
    with pytest.raises(RuntimeError, match="uncertain"):
        candidate.prepare_stage(tmp_path, "testpypi")
    assert state["testpypi_dispatch"]["deadline"] == clock[0] + 300
    monkeypatch.setattr(
        candidate,
        "_runs",
        lambda *args: [{"databaseId": 99, "status": "in_progress", "conclusion": None}],
    )
    with pytest.raises(RuntimeError, match="pending"):
        candidate.prepare_stage(tmp_path, "testpypi")
    assert state["testpypi_run"] == 99
    assert not any("workflow" in call for call in calls)


@pytest.mark.parametrize("failure", ["api", "index", "published"])
def test_retry_rechecks_fail_closed_before_dispatch(
    dispatch,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    state, calls, clock = dispatch
    state["testpypi_requested"] = "test-head"
    with pytest.raises(RuntimeError, match="uncertain"):
        candidate.prepare_stage(tmp_path, "testpypi")
    clock[0] += 300
    count = [0]

    def lookup(*args):
        count[0] += 1
        if count[0] == 1:
            return {}
        if failure == "published":
            return {"package.whl": "published"}
        message = "index unavailable"
        raise RuntimeError(message)

    if failure == "api":
        monkeypatch.setattr(
            candidate,
            "_runs",
            lambda *args: (_ for _ in ()).throw(RuntimeError("api unavailable")),
        )
    else:
        monkeypatch.setattr(candidate, "index_files", lookup)
    with pytest.raises(RuntimeError, match=r"unavailable|retained"):
        candidate.prepare_stage(tmp_path, "testpypi")
    assert not any("workflow" in call for call in calls)


def test_concurrent_retry_is_excluded_and_lock_is_released(
    dispatch,
    tmp_path: Path,
) -> None:
    state, calls, _clock = dispatch
    lock = candidate.release_preparation.release_lock(tmp_path)
    with lock, pytest.raises(RuntimeError, match="Another release action"):
        candidate.prepare_stage(tmp_path, "testpypi")
    assert not any("workflow" in call for call in calls)
    with pytest.raises(RuntimeError, match="workflow requested"):
        candidate.prepare_stage(tmp_path, "testpypi")
    assert state["testpypi_dispatch"]["outcome"] == "acknowledged"


@pytest.mark.parametrize(
    "conflict",
    ["published", "tag", "ref", "active", "uncertain", "missing"],
)
def test_old_publication_must_be_reconciled_before_head_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    conflict: str,
) -> None:
    state = {"version": "1.2.3.4", "candidate_sha": "old", "testpypi_sha": "test-head"}
    calls = []
    monkeypatch.setattr(
        candidate,
        "index_files",
        lambda *args: {"file": "hash"} if conflict == "published" else {},
    )

    def run(root, *args):
        calls.append(args)
        if args[:3] == ("git", "ls-remote", "--tags") and conflict == "tag":
            return "old refs/tags/v1.2.3.4"
        if args[:3] == ("git", "ls-remote", "--heads"):
            return ("other" if conflict == "ref" else "test-head") + " refs/heads/testpypi-1.2.3.4"
        if args == ("git", "rev-list", "--parents", "-n", "1", "test-head"):
            return "test-head old"
        if args[:2] == ("git", "show"):
            metadata = '[project]\nname = "analytics-toolkit"\nversion = "1.2.3.4"'
            return (
                candidate.replace_project_name_text(
                    metadata,
                    candidate.PROJECTS["testpypi"],
                ).strip()
                if args[2].startswith("test-head:")
                else metadata
            )
        if args[:2] == ("git", "diff"):
            return "pyproject.toml"
        return ""

    monkeypatch.setattr(candidate.sessions, "run", run)
    monkeypatch.setattr(
        candidate,
        "_runs",
        lambda *args: [{"databaseId": 42, "status": "in_progress"}] if conflict == "active" else [],
    )
    monkeypatch.setattr(candidate.release_preparation, "save", lambda *args: None)
    if conflict == "uncertain":
        state["testpypi_requested"] = "test-head"
    if conflict == "missing":
        state["testpypi_run"] = 42
    with pytest.raises(
        RuntimeError,
        match=r"new reviewed version|reconciled|active|uncertain|unavailable",
    ):
        candidate.reconcile_replacement(tmp_path, state)
    assert not any("push" in call for call in calls)


def test_run_appearing_at_final_recheck_is_adopted_without_dispatch(
    dispatch,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state, calls, _clock = dispatch
    lookups = [
        [],
        [
            {"databaseId": 99, "status": "in_progress", "conclusion": None},
        ],
    ]

    def runs(*args):
        return lookups.pop(0) if len(lookups) > 1 else lookups[0]

    monkeypatch.setattr(candidate, "_runs", runs)
    with pytest.raises(RuntimeError, match="pending"):
        candidate.prepare_stage(tmp_path, "testpypi")
    assert state["testpypi_run"] == 99
    assert not any("workflow" in call for call in calls)


def test_acknowledged_dispatch_is_not_repeated_before_deadline(
    dispatch,
    tmp_path: Path,
) -> None:
    state, calls, _clock = dispatch
    with pytest.raises(RuntimeError, match="workflow requested"):
        candidate.prepare_stage(tmp_path, "testpypi")
    deadline = state["testpypi_dispatch"]["deadline"]
    for _attempt in range(2):
        with pytest.raises(RuntimeError, match="uncertain"):
            candidate.prepare_stage(tmp_path, "testpypi")
    assert sum("workflow" in call for call in calls) == 1
    assert state["testpypi_dispatch"]["deadline"] == deadline


def test_completed_unpublished_old_attempt_removes_only_leased_temporary_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = {
        "version": "1.2.3.4",
        "candidate_sha": "old",
        "testpypi_sha": "test-head",
        "testpypi_run": 42,
    }
    calls = []
    monkeypatch.setattr(candidate, "index_files", lambda *args: {})
    monkeypatch.setattr(
        candidate,
        "_runs",
        lambda root, ref, sha, stage: (
            [{"databaseId": 42, "status": "completed"}] if stage == "testpypi" else []
        ),
    )

    def run(root, *args):
        calls.append(args)
        if args[:3] == ("git", "ls-remote", "--heads"):
            return "test-head refs/heads/testpypi-1.2.3.4"
        if args == ("git", "rev-list", "--parents", "-n", "1", "test-head"):
            return "test-head old"
        if args[:2] == ("git", "show"):
            metadata = '[project]\nname = "analytics-toolkit"\nversion = "1.2.3.4"'
            return (
                candidate.replace_project_name_text(
                    metadata,
                    candidate.PROJECTS["testpypi"],
                ).strip()
                if args[2].startswith("test-head:")
                else metadata
            )
        if args[:2] == ("git", "diff"):
            return "pyproject.toml"
        return ""

    monkeypatch.setattr(candidate.sessions, "run", run)
    monkeypatch.setattr(
        candidate.release_preparation,
        "state_path",
        lambda root: root / "state.json",
    )
    monkeypatch.setattr(candidate, "_jobs", lambda *args: {"publish to TestPyPI": "failure"})
    candidate.reconcile_replacement(tmp_path, state)
    assert calls[-1] == (
        "git",
        "push",
        "--force-with-lease=refs/heads/testpypi-1.2.3.4:test-head",
        "origin",
        ":testpypi-1.2.3.4",
    )


def test_successful_old_upload_waits_for_index_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = {"version": "1.2.3.4", "candidate_sha": "old", "testpypi_sha": "test-head"}
    monkeypatch.setattr(candidate, "index_files", lambda *args: {})

    def run(root, *args):
        if args[:3] == ("git", "ls-remote", "--heads"):
            return "test-head refs/heads/testpypi-1.2.3.4"
        if args == ("git", "rev-list", "--parents", "-n", "1", "test-head"):
            return "test-head old"
        if args[:2] == ("git", "show"):
            metadata = '[project]\nname = "analytics-toolkit"\nversion = "1.2.3.4"'
            return (
                candidate.replace_project_name_text(
                    metadata,
                    candidate.PROJECTS["testpypi"],
                ).strip()
                if args[2].startswith("test-head:")
                else metadata
            )
        if args[:2] == ("git", "diff"):
            return "pyproject.toml"
        return ""

    monkeypatch.setattr(candidate.sessions, "run", run)
    monkeypatch.setattr(candidate.release_preparation, "save", lambda *args: None)
    monkeypatch.setattr(
        candidate,
        "_runs",
        lambda *args: [{"databaseId": 42, "status": "completed"}],
    )
    monkeypatch.setattr(candidate, "_jobs", lambda *args: {"publish to TestPyPI": "success"})
    with pytest.raises(RuntimeError, match="upload succeeded"):
        candidate.reconcile_replacement(tmp_path, state)
