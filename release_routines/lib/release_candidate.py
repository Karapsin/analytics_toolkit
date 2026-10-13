"""Fail-closed upload guards and recovery using the candidate's retained artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from typing import Any

from agent_tools import release_preparation, sessions, task_context
from release_routines.lib.project_metadata import load_project
from release_routines.lib.testpypi_metadata import replace_project_name_text

PROJECTS = {"testpypi": "karapsin-analytics-toolkit", "pypi": "analytics-toolkit"}
INDEXES = {"testpypi": "https://test.pypi.org", "pypi": "https://pypi.org"}
DISTRIBUTIONS_COUNT = 2
DISPATCH_RECONCILIATION_SECONDS = 5 * 60


def index_files(stage: str, version: str) -> dict[str, str]:
    url = f"{INDEXES[stage]}/pypi/{PROJECTS[stage]}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - fixed HTTPS indexes.
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == HTTPStatus.NOT_FOUND:
            return {}
        message = "Package index lookup failed; publication is blocked."
        raise RuntimeError(message) from exc
    except (OSError, ValueError) as exc:
        message = "Package index lookup failed; publication is blocked."
        raise RuntimeError(message) from exc
    try:
        files = {item["filename"]: item["digests"]["sha256"] for item in payload["urls"]}
    except (KeyError, TypeError, ValueError) as exc:
        message = "Package index metadata is incomplete; publication is blocked."
        raise RuntimeError(message) from exc
    if not files:
        message = "Package index metadata is incomplete; publication is blocked."
        raise RuntimeError(message)
    return files


def guard(root: Path, *, production: bool = False) -> dict[str, Any]:
    state = release_preparation.verify(root)
    if load_project(root / "pyproject.toml")["name"] != "analytics-toolkit":
        message = "Release requires production project metadata."
        raise RuntimeError(message)
    mcp = release_preparation.mcp_runtime()
    receipts = (  # repository-private exact-tree validation receipts
        mcp._verify_precommit_success(root),  # noqa: SLF001
        mcp._verify_release_check_success(root),  # noqa: SLF001
    )
    for verification in receipts:
        if not verification["ok"]:
            raise RuntimeError(verification["message"])
    if production and state["stages"].get("testpypi_verified") != state["candidate_sha"]:
        message = "Production upload requires verified TestPyPI artifacts for this candidate."
        raise RuntimeError(message)
    return state


def artifact_hashes(root: Path, run_id: int) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="atk-release-artifacts-") as directory:
        sessions.run(
            root, "gh", "run", "download", str(run_id), "--name", "distributions",
            "--dir", directory,
        )
        return distribution_hashes(Path(directory))


def distribution_hashes(directory: Path) -> dict[str, str]:
    files = list(directory.iterdir())
    if (
        len(files) != DISTRIBUTIONS_COUNT
        or not any(path.name.endswith(".whl") for path in files)
        or not any(path.name.endswith(".tar.gz") for path in files)
        or any(not path.is_file() for path in files)
    ):
        message = "Retained verified wheel and sdist are required for recovery."
        raise RuntimeError(message)
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def check_upload_files(root: Path, stage: str) -> None:
    version = load_project(root / "pyproject.toml")["version"]
    compare_artifacts(index_files(stage, version), distribution_hashes(root / "dist"))


def compare_artifacts(published: dict[str, str], artifacts: dict[str, str]) -> bool:
    if any(artifacts.get(name) != digest for name, digest in published.items()):
        message = "Published artifacts differ; prepare a new version."
        raise RuntimeError(message)
    return bool(artifacts) and published == artifacts


def _verify_test_branch(root: Path, state: dict[str, Any], head: str) -> None:
    """Accept only a single-parent commit changing the temporary project name."""
    if state.get("testpypi_sha") and state["testpypi_sha"] != head:
        message = "Temporary ref cannot be safely reconciled with the old candidate."
        raise RuntimeError(message)
    parents = sessions.run(root, "git", "rev-list", "--parents", "-n", "1", head).split()
    if parents != [head, state["candidate_sha"]]:
        message = "Existing TestPyPI branch belongs to a different candidate."
        raise RuntimeError(message)
    expected = sessions.run(root, "git", "show", state["candidate_sha"] + ":pyproject.toml")
    actual = sessions.run(root, "git", "show", head + ":pyproject.toml")
    if actual != replace_project_name_text(expected, PROJECTS["testpypi"]).strip():
        message = "Existing TestPyPI branch has changed metadata."
        raise RuntimeError(message)
    paths = sessions.run(root, "git", "diff", "--name-only", state["candidate_sha"], head)
    if paths != "pyproject.toml":
        message = "TestPyPI branch contains changes beyond the temporary project name."
        raise RuntimeError(message)


def _test_local_ref(state: dict[str, Any]) -> str:
    return "refs/atk-release/testpypi/" + state["candidate_sha"]


def _recover_test_branch(
    root: Path, state: dict[str, Any], *, require_remote: bool = True,
) -> str | None:
    branch = "testpypi-" + state["version"]
    remote = sessions.run(root, "git", "ls-remote", "--heads", "origin", branch)
    if not remote and state.get("testpypi_push_pending"):
        head = state["testpypi_sha"]
        retained = sessions.run(root, "git", "rev-parse", _test_local_ref(state))
        if retained != head:
            message = "Retained TestPyPI commit differs from the journaled push."
            raise RuntimeError(message)
        _verify_test_branch(root, state, head)
        # The empty lease forbids replacing even an ancestor pushed concurrently.
        sessions.run(
            root, "git", "push", f"--force-with-lease=refs/heads/{branch}:",
            "origin", f"{head}:refs/heads/{branch}",
        )
        remote = sessions.run(root, "git", "ls-remote", "--heads", "origin", branch)
        if not remote:
            message = "Pending TestPyPI push is not visible; retry recovery."
            raise RuntimeError(message)
    if remote:
        head = remote.split()[0]
        sessions.run(root, "git", "fetch", "origin", head)
        _verify_test_branch(root, state, head)
        # Persist legacy adoption before any workflow/publication reconciliation.
        state["testpypi_sha"] = head
        state.pop("testpypi_push_pending", None)
        release_preparation.save(root, state)
        return head
    removed = (
        not require_remote
        and state.get("testpypi_removal_pending") == state.get("testpypi_sha")
    )
    if state.get("testpypi_sha") and not removed:
        message = "Recorded TestPyPI branch disappeared; preserve candidate artifacts."
        raise RuntimeError(message)
    # Replacement can already have deleted a reconciled ref under its lease.
    return None


def _test_branch(root: Path, state: dict[str, Any]) -> str:
    head = _recover_test_branch(root, state)
    if head:
        return head
    branch = "testpypi-" + state["version"]
    with tempfile.TemporaryDirectory(prefix="atk-testpypi-") as directory:
        work = Path(directory) / "checkout"
        remote_url = sessions.run(root, "git", "remote", "get-url", "origin")
        sessions.run(root, "git", "clone", remote_url, str(work))
        # Squash merges and branch deletion leave the PR head outside a fresh clone.
        sessions.run(work, "git", "fetch", "origin", f"refs/pull/{state['pr']}/head")
        fetched = sessions.run(work, "git", "rev-parse", "FETCH_HEAD")
        tree = sessions.run(work, "git", "rev-parse", "FETCH_HEAD^{tree}")
        if fetched != state["candidate_sha"] or tree != state["candidate_tree"]:
            message = "Fetched preparation head differs from the verified candidate."
            raise RuntimeError(message)
        sessions.run(work, "git", "switch", "-c", branch, fetched)
        sessions.run(
            work, "python", "-m", "release_routines.lib.testpypi_metadata",
            "replace-project-name", PROJECTS["testpypi"],
        )
        sessions.run(work, "git", "add", "--", "pyproject.toml")
        # Use the release preparation identity, preserving enrollment and signing.
        preparation = sessions.load(Path(state["workspace"]))
        sessions.save(work, {"signing_key": preparation["signing_key"]})
        sessions.run(
            work, "git", *sessions.commit_options(work), "commit", "-m",
            "Use TestPyPI package name for " + state["version"],
        )
        head = sessions.run(work, "git", "rev-parse", "HEAD")
        # Retain the exact signed commit before the temporary checkout is removed.
        sessions.run(root, "git", "fetch", str(work), head)
        sessions.run(root, "git", "update-ref", _test_local_ref(state), head)
        state["testpypi_sha"] = head
        state["testpypi_push_pending"] = True
        release_preparation.save(root, state)
        recovered = _recover_test_branch(root, state)
        if not recovered:
            message = "TestPyPI branch publication is incomplete."
            raise RuntimeError(message)
        return recovered


def _production_ref(root: Path, state: dict[str, Any]) -> str:
    tag = "v" + state["version"]
    sessions.run(root, "git", "fetch", "origin", "--tags")
    remote = sessions.run(root, "git", "ls-remote", "--tags", "origin", tag, tag + "^{}")
    if remote:
        lines = remote.splitlines()
        tagged = next((line.split()[0] for line in lines if line.endswith("^{}")), lines[0].split()[0])
        sessions.run(root, "git", "fetch", "origin", tagged)
        if tagged != sessions.run(root, "git", "rev-parse", "HEAD"):
            message = "Existing production tag belongs to different content."
            raise RuntimeError(message)
    else:
        # Never force or move even a local existing tag.
        local = sessions.run(root, "git", "tag", "--list", tag)
        if local:
            if sessions.run(root, "git", "rev-list", "-n", "1", tag) != sessions.run(
                root, "git", "rev-parse", "HEAD",
            ):
                message = "Existing local tag differs from the prepared candidate."
                raise RuntimeError(message)
        else:
            sessions.run(root, "git", "tag", tag)
        sessions.run(root, "git", "push", "origin", "refs/tags/" + tag)
    return tag


def _runs(root: Path, ref: str, sha: str, stage: str) -> list[dict[str, Any]]:
    pages = json.loads(
        sessions.run(
            root,
            "gh",
            "api",
            "--method",
            "GET",
            "repos/{owner}/{repo}/actions/workflows/publish.yml/runs",
            "-f",
            f"branch={ref}",
            "-f",
            f"head_sha={sha}",
            "-f",
            "event=release" if stage == "pypi" else "event=workflow_dispatch",
            "-F",
            "per_page=100",
            "--paginate",
            "--slurp",
        )
    )
    rows = [run for page in pages for run in page["workflow_runs"]]
    if pages and max(page["total_count"] for page in pages) > len(rows):
        message = "Candidate workflow lookup is incomplete; publication is blocked."
        raise RuntimeError(message)
    return [
        {
            "databaseId": run["id"],
            "headSha": run["head_sha"],
            "status": run["status"],
            "conclusion": run["conclusion"],
        }
        for run in rows
        if run["head_sha"] == sha
    ]


def _jobs(root: Path, run_id: int) -> dict[str, str]:
    # Failed-only reruns may omit the original successful build from the latest
    # attempt. Preserve that evidence while taking the latest result per job.
    pages = json.loads(
        sessions.run(
            root,
            "gh",
            "api",
            f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs?filter=all&per_page=100",
            "--paginate",
            "--slurp",
        )
    )
    jobs = sorted((job for page in pages for job in page["jobs"]), key=lambda job: job["id"])
    return {job["name"]: job.get("conclusion") or "" for job in jobs}


def _dispatch_record(root: Path, state: dict[str, Any], ref: str, sha: str) -> dict[str, Any]:
    record: dict[str, Any] = state.get("testpypi_dispatch") or {}
    if state.get("testpypi_requested") and state["testpypi_requested"] != sha:
        message = "Legacy dispatch marker belongs to another candidate."
        raise RuntimeError(message)
    if not record and state.get("testpypi_requested"):
        now = time.time()
        record = {
            "candidate_sha": state["candidate_sha"], "ref": ref, "sha": sha,
            "attempted_at": now, "deadline": now + DISPATCH_RECONCILIATION_SECONDS,
            "outcome": "uncertain",
        }
        state["testpypi_dispatch"] = record
        release_preparation.save(root, state)
    if record and (
        record["candidate_sha"] != state["candidate_sha"]
        or record["ref"] != ref or record["sha"] != sha
    ):
        message = "Dispatch record belongs to another candidate; reconcile it before replacement."
        raise RuntimeError(message)
    return record or {}


def _dispatch_testpypi(
    root: Path, state: dict[str, Any], ref: str, sha: str,
) -> list[dict[str, Any]]:
    record = _dispatch_record(root, state, ref, sha)
    if record and record["outcome"] != "failed" and time.time() < record["deadline"]:
        message = "Candidate dispatch is uncertain; retry reconciliation within five minutes."
        raise RuntimeError(message)
    # Recheck immediately before the replacement, under the candidate lock.
    runs = _runs(root, ref, sha, "testpypi")
    if runs:
        return runs
    if index_files("testpypi", state["version"]):
        message = "Published version has no matching retained candidate artifacts."
        raise RuntimeError(message)
    now = time.time()
    state["testpypi_dispatch"] = {
        "candidate_sha": state["candidate_sha"], "ref": ref, "sha": sha,
        "attempted_at": now, "deadline": now + DISPATCH_RECONCILIATION_SECONDS,
        "outcome": "uncertain",
    }
    state.pop("testpypi_requested", None)
    release_preparation.save(root, state)  # Interruption before dispatch is recoverable.
    try:
        sessions.run(root, "gh", "workflow", "run", "publish.yml", "--ref", ref)
    except RuntimeError as exc:
        # Only explicit rejection proves no dispatch was accepted. Network failures,
        # timeouts and interrupted acknowledgements retain the bounded uncertainty.
        if any(code in str(exc) for code in ("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404", "HTTP 422")):
            state["testpypi_dispatch"]["outcome"] = "failed"
            release_preparation.save(root, state)
        raise
    state["testpypi_dispatch"]["outcome"] = "acknowledged"
    release_preparation.save(root, state)
    return []


def reconcile_replacement(  # noqa: C901, PLR0912, PLR0915 - publication reconciliation.
    root: Path,
    state: dict[str, Any],
    *,
    allow_reserved: bool = False,
) -> bool:
    """Block candidate replacement until old publications and refs are reconciled."""
    published = {stage: index_files(stage, state["version"]) for stage in PROJECTS}
    tag = "v" + state["version"]
    remote_tag = sessions.run(root, "git", "ls-remote", "--tags", "origin", tag, tag + "^{}")
    local_tag = sessions.run(root, "git", "tag", "--list", tag)
    reserved = bool(any(published.values()) or remote_tag or local_tag)
    if reserved and not allow_reserved:
        message = "Published artifacts or production tags require a new reviewed version."
        raise RuntimeError(message)
    production_sha = state["candidate_sha"]
    if remote_tag:
        lines = remote_tag.splitlines()
        production_sha = next(
            (line.split()[0] for line in lines if line.endswith("^{}")),
            lines[0].split()[0],
        )
    elif local_tag:
        production_sha = sessions.run(root, "git", "rev-list", "-n", "1", tag)
    ref = "testpypi-" + state["version"]
    _recover_test_branch(root, state, require_remote=False)
    remote = sessions.run(root, "git", "ls-remote", "--heads", "origin", ref)
    sha = state.get("testpypi_sha")
    if remote and remote.split()[0] != sha:
        message = "Temporary ref cannot be safely reconciled with the old candidate."
        raise RuntimeError(message)
    for stage, branch, head in (
        ("testpypi", ref, sha),
        ("pypi", tag, production_sha),
    ):
        runs = _runs(root, branch, head, stage) if head else []
        saved = state.get(stage + "_run")
        if saved and not any(run["databaseId"] == saved for run in runs):
            message = "Recorded candidate workflow is unavailable; replacement is blocked."
            raise RuntimeError(message)
        if any(run["status"] != "completed" for run in runs):
            message = "Old candidate publication is active; reconcile it before replacement."
            raise RuntimeError(message)
        if stage == "pypi" and remote_tag and not runs:
            releases = json.loads(
                sessions.run(
                    root,
                    "gh",
                    "api",
                    "repos/{owner}/{repo}/releases",
                    "--paginate",
                    "--slurp",
                )
            )
            if any(release["tag_name"] == tag for page in releases for release in page):
                message = "Existing release publication is uncertain; replacement is blocked."
                raise RuntimeError(message)
        retained = False
        for run in runs:
            jobs = _jobs(root, int(run["databaseId"]))
            target = "publish to PyPI" if stage == "pypi" else "publish to TestPyPI"
            if published[stage] and jobs.get("build distribution") == "success":
                # Inspect retained artifacts even for a partial or conflicting
                # upload. Conflicting hashes require a fresh version, never reuse.
                artifacts = artifact_hashes(root, int(run["databaseId"]))
                try:
                    compare_artifacts(published[stage], artifacts)
                except RuntimeError:
                    if not allow_reserved:
                        raise
                retained = True
            if jobs.get(target) == "success" and not published[stage]:
                message = (
                    "Old candidate upload succeeded; reconcile published artifacts "
                    "before replacement."
                )
                raise RuntimeError(message)
        if published[stage] and not retained:
            message = (
                "Published artifacts lack matching retained workflow evidence; "
                "replacement is blocked."
            )
            raise RuntimeError(message)
        if state.get("stages", {}).get(stage + "_verified") and not published[stage]:
            message = "Previously verified publication is unavailable; replacement is blocked."
            raise RuntimeError(message)
        if stage == "testpypi" and head:
            record = _dispatch_record(root, state, ref, head)
            if (
                record
                and not runs
                and record["outcome"] != "failed"
                and time.time() < record["deadline"]
            ):
                message = "Old candidate dispatch is uncertain; reconcile it before replacement."
                raise RuntimeError(message)
    # Remove only an unpublished, reconciled name-only ref, under a lease.
    if remote and not reserved:
        # Journal the reconciled deletion so a restart after it remains resumable.
        state["testpypi_removal_pending"] = sha
        release_preparation.save(root, state)
        sessions.run(
            root,
            "git",
            "push",
            f"--force-with-lease=refs/heads/{ref}:{sha}",
            "origin",
            ":" + ref,
        )
    return reserved


def prepare_stage(root: Path, stage: str) -> int:
    with release_preparation.release_lock(root):
        return _prepare_stage(root, stage)


def _prepare_stage(  # noqa: C901, PLR0912, PLR0915 - resumable publish stages.
    root: Path, stage: str,
) -> int:
    state = guard(root, production=stage == "pypi")
    published = index_files(stage, state["version"])  # Network errors never mean available.
    if state["stages"].get(stage + "_verified") == state["candidate_sha"]:
        run_id = int(state[stage + "_run"])
        if not compare_artifacts(published, artifact_hashes(root, run_id)):
            message = "Previously verified index artifacts are unavailable."
            raise RuntimeError(message)
        return run_id
    if stage == "testpypi":
        sha = _test_branch(root, state)
        ref = "testpypi-" + state["version"]
        state["testpypi_sha"] = sha
    else:
        ref = _production_ref(root, state)
        sha = sessions.run(root, "git", "rev-list", "-n", "1", ref)
    release_preparation.save(root, state)
    runs = _runs(root, ref, sha, stage)
    saved = state.get(stage + "_run")
    if saved and not any(run["databaseId"] == saved for run in runs):
        message = "Recorded candidate workflow is unavailable; do not rebuild published artifacts."
        raise RuntimeError(message)
    if not runs:
        if published:
            message = "Published version has no matching retained candidate artifacts."
            raise RuntimeError(message)
        # API failures are not treated as an absent release.
        if stage == "pypi":
            releases = json.loads(
                sessions.run(
                    root,
                    "gh",
                    "api",
                    "repos/{owner}/{repo}/releases",
                    "--paginate",
                    "--slurp",
                )
            )
            releases = [release for page in releases for release in page]
            if any(release["tag_name"] == ref for release in releases):
                message = "Existing release has no matching workflow yet; retry after GitHub schedules it."
                raise RuntimeError(message)
            sessions.run(
                root, "gh", "release", "create", ref, "--title", ref,
                "--notes", "Release " + state["version"],
            )
        else:
            recovered = _dispatch_testpypi(root, state, ref, sha)
            if recovered:
                state["testpypi_run"] = int(recovered[0]["databaseId"])
                release_preparation.save(root, state)
                return _prepare_stage(root, stage)
        message = "Publish workflow requested; re-enter the release action to resume this candidate."
        raise RuntimeError(message)
    run = next((run for run in runs if run["databaseId"] == saved), runs[0])
    run_id = int(run["databaseId"])
    state[stage + "_run"] = run_id
    release_preparation.save(root, state)
    if run["status"] != "completed":
        message = "Candidate publish workflow is pending; re-enter the release action to resume."
        raise RuntimeError(message)
    jobs = _jobs(root, run_id)
    build_ok = jobs.get("build distribution") == "success"
    artifacts = artifact_hashes(root, run_id) if build_ok else {}
    if published and not build_ok:
        message = "Published version lacks verified artifacts; prepare a new version."
        raise RuntimeError(message)
    try:
        complete = compare_artifacts(published, artifacts) if artifacts else False
    except RuntimeError:
        state["requires_new_version"] = True
        release_preparation.save(root, state)
        raise
    if not complete and run["conclusion"] != "success":
        if build_ok:
            # This confirms retained verified artifacts exist before retrying only
            # failed jobs. Successful build jobs are never rerun for publication.
            artifact_hashes(root, run_id)
        sessions.run(root, "gh", "run", "rerun", str(run_id), "--failed")
        message = "Failed candidate jobs requested again; re-enter the release action to resume."
        raise RuntimeError(message)
    if not complete:
        message = "Uploaded artifacts are not yet available in index metadata; retry verification."
        raise RuntimeError(message)
    other = "publish to TestPyPI" if stage == "pypi" else "publish to PyPI"
    if not build_ok or jobs.get(other) != "skipped":
        message = "Unexpected build or opposite-index job conclusion."
        raise RuntimeError(message)
    return run_id


def verified(root: Path, stage: str) -> None:
    with release_preparation.release_lock(root):
        _verified(root, stage)


def _verified(root: Path, stage: str) -> None:
    state = guard(root, production=stage == "pypi")
    run_id = state.get(stage + "_run")
    if not run_id or not compare_artifacts(
        index_files(stage, state["version"]), artifact_hashes(root, run_id),
    ):
        message = "Cannot record verification without the candidate's published artifacts."
        raise RuntimeError(message)
    state["stages"][stage + "_verified"] = state["candidate_sha"]
    release_preparation.save(root, state)
    if stage == "pypi":
        active = task_context.directory(root) / "active.json"
        if active.exists() and task_context.read(root)["task_id"] == state["task_id"]:
            task_context.finish(root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("guard", "prepare-stage", "verified", "check-artifacts"))
    parser.add_argument("--stage", choices=tuple(PROJECTS), default="testpypi")
    args = parser.parse_args()
    root = Path.cwd()
    try:
        if args.action == "guard":
            guard(root, production=args.stage == "pypi")
        elif args.action == "prepare-stage":
            print(prepare_stage(root, args.stage))
        elif args.action == "check-artifacts":
            check_upload_files(root, args.stage)
        else:
            verified(root, args.stage)
    except (RuntimeError, ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
