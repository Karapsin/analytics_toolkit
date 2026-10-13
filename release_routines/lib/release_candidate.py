"""Fail-closed upload guards and recovery using the candidate's retained artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
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


def _test_branch(root: Path, state: dict[str, Any]) -> str:
    branch = "testpypi-" + state["version"]
    remote = sessions.run(root, "git", "ls-remote", "--heads", "origin", branch)
    if remote:
        head = remote.split()[0]
        sessions.run(root, "git", "fetch", "origin", head)
        if sessions.run(root, "git", "rev-parse", head + "^") != state["candidate_sha"]:
            message = "Existing TestPyPI branch belongs to a different candidate."
            raise RuntimeError(message)
        expected = sessions.run(root, "git", "show", state["candidate_sha"] + ":pyproject.toml")
        actual = sessions.run(root, "git", "show", head + ":pyproject.toml")
        # Only the project name may differ, never package content.
        if actual != replace_project_name_text(expected, PROJECTS["testpypi"]).strip():
            message = "Existing TestPyPI branch has changed metadata."
            raise RuntimeError(message)
        paths = sessions.run(root, "git", "diff", "--name-only", state["candidate_sha"], head)
        if paths != "pyproject.toml":
            message = "TestPyPI branch contains changes beyond the temporary project name."
            raise RuntimeError(message)
        return head
    if state.get("testpypi_sha"):
        message = "Recorded TestPyPI branch disappeared; preserve candidate artifacts."
        raise RuntimeError(message)
    with tempfile.TemporaryDirectory(prefix="atk-testpypi-") as directory:
        work = Path(directory) / "checkout"
        remote_url = sessions.run(root, "git", "remote", "get-url", "origin")
        sessions.run(root, "git", "clone", remote_url, str(work))
        sessions.run(work, "git", "switch", "-c", branch, state["candidate_sha"])
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
        sessions.run(work, "git", "push", "origin", f"HEAD:refs/heads/{branch}")
        return head


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
    runs: list[dict[str, Any]] = json.loads(
        sessions.run(
            root, "gh", "run", "list", "--workflow", "publish.yml", "--branch", ref,
            "--event", "release" if stage == "pypi" else "workflow_dispatch",
            "--commit", sha, "--limit", "100", "--json",
            "databaseId,headSha,status,conclusion",
        )
    )
    return runs


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


def prepare_stage(  # noqa: C901, PLR0912, PLR0915 - resumable publish stages.
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
            if state.get("testpypi_requested"):
                message = "Candidate workflow request is not yet visible; do not dispatch a duplicate."
                raise RuntimeError(message)
            state["testpypi_requested"] = sha
            release_preparation.save(root, state)
            sessions.run(root, "gh", "workflow", "run", "publish.yml", "--ref", ref)
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
