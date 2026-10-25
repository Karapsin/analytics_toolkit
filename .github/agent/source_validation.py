"""Run a fixed reviewed container transport; emit non-production evidence only."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import uuid
from typing import Any

REPOSITORY = "Karapsin/analytics_toolkit"
TRANSPORT = "467768b9bed2c90a0dfba85ef2eea6ac6868f8d6"
SOURCES = {
    ".github/agent/capture_isolation.py": "6aa4f93fbf0037a50a04217df8012f51ee5782fceb9e856f129fe1c9391bf440",
    ".github/agent/capture_sandbox.py": "4dc6c61dc7da17f24fd6d038cfe0865f50085f1de3c330b590f39490a58fefae",
    ".github/agent/capture_artifact.py": "7a032cfbab17109dc41312d1565a75cca53910bcc68805000a8aba850ac9f89b",
    "agent_tools/sql_explorer_visual_render.py": "2284b4c7bc162ed0c408ed033c637a3adcd4743cab615aab6ecf44e67fe0ac07",
}
SHA = re.compile(r"[0-9a-f]{40}")
OUTPUT_LIMIT = 4096


def directory(path: Path) -> Path:
    """Admit an owned real directory without following any symlink component."""
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError("Source-validation directory is not a real absolute path")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Source-validation directory ownership is unavailable")
    return path


def regular(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.getuid()
            or before.st_size > limit
        ):
            raise ValueError("Source-validation file is unsafe or exceeds capacity")
        body = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if len(body) > limit or any(
            getattr(before, name) != getattr(after, name) for name in fields
        ):
            raise ValueError("Source-validation file changed or exceeded capacity")
    return body


def identity(environment: dict[str, str]) -> dict[str, Any]:
    subject = environment.get("SOURCE_VALIDATION_SUBJECT", "")
    workflow = environment.get("GITHUB_SHA", "")
    owner = REPOSITORY.split("/")[0]
    if (
        environment.get("SOURCE_VALIDATION") != "true"
        or SHA.fullmatch(subject) is None
        or SHA.fullmatch(workflow) is None
        or environment.get("GITHUB_REPOSITORY") != REPOSITORY
        or environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or environment.get("GITHUB_ACTOR") != owner
        or environment.get("GITHUB_TRIGGERING_ACTOR") != owner
        or environment.get("GITHUB_WORKFLOW_SHA") != workflow
        or environment.get("GITHUB_REF") != "refs/tags/agent-source-validation/v1/" + workflow
        or environment.get("VISUAL_ISOLATION_PROBE", "false") == "true"
    ):
        raise ValueError("Source-validation mode, actor, frozen ref or subject is invalid")
    numbers = {}
    for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        text = environment.get(name, "")
        if re.fullmatch(r"[1-9][0-9]{0,19}", text) is None:
            raise ValueError("Source-validation run identity is invalid")
        numbers[name] = int(text)
    return {
        "repository": REPOSITORY,
        "candidate": subject,
        "workflow_sha": workflow,
        "transport_sha": TRANSPORT,
        "run_id": numbers["GITHUB_RUN_ID"],
        "run_attempt": numbers["GITHUB_RUN_ATTEMPT"],
    }


def git_head(root: Path, environment: dict[str, str]) -> str:
    directory(root)
    directory(root / ".git")
    clean = {
        "PATH": environment["PATH"],
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(root),
            "rev-parse",
            "--verify",
            "HEAD",
        ],
        env=clean,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode or SHA.fullmatch(result.stdout.strip()) is None:
        raise ValueError("Source-validation checkout identity is unavailable")
    return result.stdout.strip()


def verify_roots(
    control: Path,
    transport: Path,
    subject: Path,
    temporary: Path,
    binding: dict[str, Any],
    environment: dict[str, str],
) -> dict[str, str]:
    roots = [directory(path) for path in (control, transport, subject, temporary)]
    for index, first in enumerate(roots):
        for second in roots[index + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                raise ValueError("Source-validation roots overlap")
    for root, expected in (
        (control, binding["workflow_sha"]),
        (transport, TRANSPORT),
        (subject, binding["candidate"]),
    ):
        if git_head(root, environment) != expected:
            raise ValueError("Source-validation checkout differs from its exact binding")
    observed = {}
    for name, expected in SOURCES.items():
        path = transport / name
        directory(path.parent)
        observed[name] = hashlib.sha256(regular(path, 256 * 1024)).hexdigest()
        if observed[name] != expected:
            raise ValueError("Fixed reviewed source-validation transport differs")
    return observed


def admit_output(command_file: Path, temporary: Path) -> Path | None:
    raw = regular(command_file, OUTPUT_LIMIT)
    if not raw:
        return None  # The transport deliberately suppresses unsafe-writer output.
    text = raw.decode("utf-8")
    prefix = "artifact_root="
    if not text.startswith(prefix) or not text.endswith("\n") or text.count("\n") != 1:
        raise ValueError("Transport output must contain one bounded root assignment")
    path = Path(text[len(prefix) : -1])
    if path.parent != temporary or re.fullmatch(r"atk-visual-[0-9a-f]{32}", path.name) is None:
        raise ValueError("Transport output is outside its run-owned root")
    return directory(path)


def scenes(subject: Path) -> tuple[list[str], str]:
    path = subject / "visual-tests/sql_explorer/scenes.json"
    directory(path.parent)
    raw = regular(path, 256 * 1024)
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("scenes"), list)
        or any(not isinstance(scene, dict) for scene in data["scenes"])
    ):
        raise ValueError("Source-validation scene manifest is malformed")
    names = [scene.get("id") for scene in data["scenes"]]
    viewport = data.get("viewport", {})
    if (
        data.get("schema_version") != 1
        or len(names) != 46
        or any(
            not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_-]+", name) is None
            for name in names
        )
        or len(set(names)) != 46
        or not isinstance(viewport, dict)
        or [viewport.get(key) for key in ("columns", "rows", "width", "height")]
        != [208, 47, 1280, 800]
    ):
        raise ValueError("Source validation requires the complete fixed-size 46-scene manifest")
    return names, hashlib.sha256(raw).hexdigest()


def capture_inventory(root: Path, expected: list[str], candidate: str) -> dict[str, str]:
    """Check completeness as data; individual full-PNG verdicts remain pending."""
    artifact = directory(root / "visual-artifact")
    names = {"capture.json"} | {name + suffix for name in expected for suffix in (".png", ".json")}
    if {path.name for path in artifact.iterdir()} != names:
        raise ValueError("Source-validation product capture is incomplete")
    session = json.loads(regular(artifact / "capture.json", 1024 * 1024))
    if (
        session.get("candidate") != candidate
        or set(session.get("scenes", {})) != set(expected)
        or session.get("capture", {}).get("status") != "pass"
        or session.get("capture", {}).get("workspace_removed") is not True
    ):
        raise ValueError("Source-validation capture session differs or is incomplete")
    digests = {}
    total = 0
    for name in sorted(names):
        body = regular(artifact / name, 32 * 1024 * 1024)
        total += len(body)
        if total > 256 * 1024 * 1024:
            raise ValueError("Source-validation capture exceeds aggregate capacity")
        digests[name] = hashlib.sha256(body).hexdigest()
        if name == "capture.json":
            continue
        scene, suffix = name.rsplit(".", 1)
        field = "screenshot_sha256" if suffix == "png" else "geometry_sha256"
        if digests[name] != session["scenes"][scene].get(field):
            raise ValueError("Source-validation scene bytes differ from their session")
        if suffix == "png":
            if (
                len(body) < 24
                or body[:8] != b"\x89PNG\r\n\x1a\n"
                or body[12:16] != b"IHDR"
                or int.from_bytes(body[16:20], "big") != 1280
                or int.from_bytes(body[20:24], "big") != 800
            ):
                raise ValueError("Source-validation full PNG dimensions differ")
        else:
            geometry = json.loads(body)
            if geometry.get("scene_id") != scene or geometry.get("ok") is not True:
                raise ValueError("Source-validation scene geometry failed")
    return digests


def invoke(command: list[str], environment: dict[str, str]) -> tuple[int | None, bool]:
    """Allow the transport to clean up after interruption; never certify forced exits."""
    process = subprocess.Popen(command, env=environment, start_new_session=True)
    interrupted = False

    def stop(_number: int, _frame: Any) -> None:
        raise InterruptedError("Source validation interrupted")

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        try:
            return process.wait(timeout=29 * 60), False
        except (subprocess.TimeoutExpired, InterruptedError, KeyboardInterrupt):
            interrupted = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=120)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=30)
            return process.returncode, interrupted
    finally:
        signal.signal(signal.SIGTERM, previous)


def run(environment: dict[str, str], *, validate_only: bool = False) -> bool:
    temporary = directory(Path(environment["RUNNER_TEMP"]))
    metadata = temporary / ("atk-source-validation-" + uuid.uuid4().hex)
    metadata.mkdir(mode=0o700)
    command_file = metadata / "transport-output"
    command_file.touch(mode=0o600)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "role": "source-validation",
        "ok": False,
        "visual_approval": False,
        "production_activation": False,
        "final_job_conclusion": None,
        "uploaded_artifacts": None,
        "capture_root_admitted": False,
        "guard_passed": False,
        "capture_attempted": False,
        "transport_exit": None,
        "transport_interrupted": None,
        "stage": "identity",
    }
    root = None
    try:
        binding = identity(environment)
        manifest.update(binding)
        manifest["guard_passed"] = True
        if validate_only:
            manifest["stage"] = "identity-validated"
            return True
        manifest["stage"] = "trusted-source-verification"
        control = Path(environment["VALIDATION_CONTROL_ROOT"])
        transport = Path(environment["VALIDATION_TRANSPORT_ROOT"])
        subject = Path(environment["VALIDATION_SUBJECT_ROOT"])
        manifest["transport_sources"] = verify_roots(
            control, transport, subject, temporary, binding, environment
        )
        expected, digest = scenes(subject)
        manifest.update(subject_manifest_sha256=digest, expected_scene_count=len(expected))
        child = {
            name: environment[name]
            for name in (
                "PATH",
                "RUNNER_TEMP",
                "GITHUB_REPOSITORY",
                "GITHUB_RUN_ID",
                "GITHUB_RUN_ATTEMPT",
            )
        }
        child.update(
            CANDIDATE=binding["candidate"],
            CANDIDATE_ROOT=str(subject),
            GITHUB_OUTPUT=str(command_file),
        )
        manifest["stage"] = "contained-capture"
        manifest["capture_attempted"] = True
        code, interrupted = invoke(
            [sys.executable, "-I", str(transport / ".github/agent/capture_isolation.py")], child
        )
        manifest.update(transport_exit=code, transport_interrupted=interrupted)
        manifest["stage"] = "artifact-path-admission"
        if not interrupted:
            root = admit_output(command_file, temporary)
        manifest["capture_root_admitted"] = root is not None
        if code == 0 and not interrupted and root is not None:
            manifest["stage"] = "capture-inventory"
            manifest["capture_files"] = capture_inventory(root, expected, binding["candidate"])
            manifest["individual_png_verdicts"] = "pending"
            manifest["ok"] = True
        manifest["stage"] = "transport-finished"
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        subprocess.SubprocessError,
    ) as exc:
        manifest["error_kind"] = type(exc).__name__
    finally:
        # This is observed transport/step state, never a future upload/job verdict.
        step_success = manifest["guard_passed"] if validate_only else manifest["ok"]
        manifest["step_outcome"] = "success" if step_success else "failure"
        (metadata / "source-validation.json").write_text(json.dumps(manifest, indent=2) + "\n")
        with Path(environment["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write("metadata_root=" + str(metadata) + "\n")
            if root is not None:
                stream.write("artifact_root=" + str(root) + "\n")
    return manifest["ok"] is True


def main(arguments: list[str], environment: dict[str, str]) -> bool:
    if arguments not in ([], ["validate"]):
        return False
    return run(environment, validate_only=bool(arguments))


if __name__ == "__main__" and not main(sys.argv[1:], dict(os.environ)):
    raise SystemExit("Source validation failed; retained evidence grants no approval")
