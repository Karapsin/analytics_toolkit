"""Fixed PR39 lifecycle experiment. Its artifacts grant no production authority."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
from typing import Any
import unicodedata
import uuid

REPOSITORY = "Karapsin/analytics_toolkit"
SUBJECT = "76eb8481734ec4ffb77d71ce300787caff21b860"
PARENT_TASK = "04c60facc8294087b55ffdd496a4de82"
PARENT_DIGEST = "813e95fe9b37968b03c3a016c0992531c4413807cc334ffe03f9e942a20e8b18"
TASK = "357a59078b4f423491e915d12ca37061"
ARCHIVES = "agent_tools/task_evidence/" + TASK + "/"
PROFILES = {"3.11": "textual>=0.73,<0.74", "3.13": "textual[syntax]>=0.89.1,<0.90"}
SOURCES = {
    "capture_isolation.py": "6aa4f93fbf0037a50a04217df8012f51ee5782fceb9e856f129fe1c9391bf440",
    "capture_sandbox.py": "4dc6c61dc7da17f24fd6d038cfe0865f50085f1de3c330b590f39490a58fefae",
    "capture_artifact.py": "7a032cfbab17109dc41312d1565a75cca53910bcc68805000a8aba850ac9f89b",
}
# Actual reviewed adapter bytes, independent of the future source commit identity.
ADAPTER_SHA256 = "e4669169c9fd67a2248ede2e58db1264c2f99e12a84d181462d2caa664a33fb5"
HEX40 = re.compile(r"[0-9a-f]{40}")
MIB = 1024 * 1024
LOG_LIMIT, DATA_LIMIT, META_LIMIT = 64 * MIB, 32 * MIB, 4 * MIB
AUTHORITY = {
    "visual_approval": False,
    "production_activation": False,
    "prerequisite_satisfied": False,
}


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def directory(path: Path) -> Path:
    if not path.is_absolute() or path != path.resolve(strict=True) or "," in str(path):
        raise ValueError("Diagnostic root must be an owned real absolute directory")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Diagnostic directory ownership is unavailable")
    return path


def regular(path: Path, limit: int) -> bytes:
    directory(path.parent)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.getuid()
            or before.st_size > limit
        ):
            raise ValueError("Unsafe diagnostic source file")
        body = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
        if len(body) != before.st_size or any(
            getattr(before, k) != getattr(after, k) for k in fields
        ):
            raise ValueError("Diagnostic source changed during read")
    return body


def write_json(path: Path, value: Any, limit: int = MIB) -> None:
    body = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    if len(body) > limit:
        raise ValueError("Diagnostic metadata exceeds its reserved capacity")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


def identity(environment: dict[str, str]) -> dict[str, Any]:
    source = environment.get("GITHUB_SHA", "")
    ref = "refs/tags/agent-source-validation/v1/" + source
    line = environment.get("DIAGNOSTIC_PYTHON", "")
    if (
        environment.get("LIFECYCLE_DIAGNOSTIC") != "true"
        or environment.get("GITHUB_REPOSITORY") != REPOSITORY
        or environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or environment.get("GITHUB_ACTOR") != "Karapsin"
        or environment.get("GITHUB_TRIGGERING_ACTOR") != "Karapsin"
        or HEX40.fullmatch(source) is None
        or environment.get("GITHUB_WORKFLOW_SHA") != source
        or environment.get("GITHUB_REF") != ref
        or environment.get("SOURCE_VALIDATION", "false") != "false"
        or environment.get("SOURCE_VALIDATION_SUBJECT", "") != ""
        or environment.get("VISUAL_ISOLATION_PROBE", "false") != "false"
        or line not in PROFILES
    ):
        raise ValueError("Diagnostic mode, actor, frozen source or profile is invalid")
    workflow_ref = environment.get("GITHUB_WORKFLOW_REF")
    if (
        workflow_ref is not None
        and workflow_ref != REPOSITORY + "/.github/workflows/tests.yml@" + ref
    ):
        raise ValueError("Diagnostic workflow identity is invalid")
    numbers = []
    for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        value = environment.get(name, "")
        if re.fullmatch(r"[1-9][0-9]{0,19}", value) is None:
            raise ValueError("Diagnostic run identity is invalid")
        numbers.append(int(value))
    return {
        "schema_version": 1,
        "purpose": "pr39-lifecycle-baseline-diagnostic",
        "repository": REPOSITORY,
        "candidate": SUBJECT,
        "workflow_sha": source,
        "run_id": numbers[0],
        "run_attempt": numbers[1],
        "python_line": line,
        "parent_task_id": PARENT_TASK,
        "parent_task_digest": PARENT_DIGEST,
        "new_task_id": TASK,
    }


def separate(*paths: Path) -> None:
    for index, first in enumerate(paths):
        directory(first)
        for second in paths[index + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                raise ValueError("Diagnostic roots overlap")


def git_head(root: Path, environment: dict[str, str]) -> str:
    directory(root / ".git")
    for name in ("objects/info/alternates", "commondir", "gitdir"):
        path = root / ".git" / name
        if path.exists() or path.is_symlink():
            raise ValueError("External Git storage is forbidden")
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
        capture_output=True,
        text=True,
        timeout=30,
        env=clean,
        check=True,
    )
    value = result.stdout.strip()
    if HEX40.fullmatch(value) is None:
        raise ValueError("Diagnostic Git identity is unavailable")
    return value


def source_closure(control: Path) -> tuple[Any, dict[str, Any]]:
    observed = {}
    for name, expected in SOURCES.items():
        actual = digest(regular(control / ".github/agent" / name, MIB))
        if actual != expected:
            raise ValueError("Reviewed shared containment source differs")
        observed[name] = actual
    adapter = digest(regular(control / ".github/agent/lifecycle_diagnostic_pytest.py", MIB))
    if adapter != ADAPTER_SHA256:
        raise ValueError("Reviewed diagnostic adapter source differs")
    parent = regular(control / ARCHIVES / "parent-pr39-task.json", MIB)
    if len(parent) != 34199 or digest(parent) != PARENT_DIGEST:
        raise ValueError("Public parent task differs")
    context = regular(control / ARCHIVES / "pr39-public-planning-context.json", MIB)
    provenance = regular(control / ARCHIVES / "baseline-provenance.json", MIB)
    public = json.loads(provenance)
    if (
        public.get("candidate") != SUBJECT
        or public.get("parent_task_sha256") != PARENT_DIGEST
        or public.get("new_task_id") != TASK
        or public.get("public_context_sha256") != digest(context)
    ):
        raise ValueError("Public diagnostic provenance differs")
    path = control / ".github/agent/capture_isolation.py"
    spec = importlib.util.spec_from_file_location("lifecycle_containment", path)
    if spec is None or spec.loader is None:
        raise ValueError("Reviewed containment loader is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, {
        "shared_sources": observed,
        "adapter_sha256": adapter,
        "public_context_sha256": digest(context),
        "provenance_sha256": digest(provenance),
        "subject_files": public["subject_files"],
        "expected_case_ids": public["expected_case_ids"],
    }


def emit(environment: dict[str, str], name: str, root: Path) -> None:
    if re.fullmatch(r"[A-Za-z0-9_]+", name) is None or "\n" in str(root):
        raise ValueError("Invalid host output assignment")
    with Path(environment["GITHUB_OUTPUT"]).open("a") as stream:
        stream.write(name + "=" + str(root) + "\n")


def guard(environment: dict[str, str]) -> bool:
    temporary = directory(Path(environment["RUNNER_TEMP"]))
    root = temporary / ("atk-lifecycle-guard-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    report: dict[str, Any] = {"ok": False, **AUTHORITY}
    try:
        binding = identity(environment)
        control = directory(Path(environment["DIAGNOSTIC_CONTROL_ROOT"]))
        separate(control, temporary)
        if git_head(control, environment) != binding["workflow_sha"]:
            raise ValueError("Control checkout differs from frozen source")
        _module, closure = source_closure(control)
        report.update({"binding": binding, "closure": closure, "ok": True})
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    write_json(root / "guard.json", report)
    # Guard-only data are closed now and never share a directory with active writers.
    emit(environment, "metadata_root", root)
    return report["ok"] is True


def copy_subject(subject: Path, destination: Path) -> None:
    shutil.copytree(subject, destination, symlinks=True)
    directory(destination / ".git")
    config = destination / ".git/config"
    regular(config, MIB)
    config.write_text(
        "[core]\nrepositoryformatversion = 0\nfilemode = true\nbare = false\n"
        "logallrefupdates = true\nhooksPath = /dev/null\n"
        '[remote "origin"]\nurl = https://github.com/'
        + REPOSITORY
        + ".git\nfetch = +refs/heads/*:refs/remotes/origin/*\n"
    )


def bounded_inventory(
    root: Path, artifact: Any, *, byte_limit: int, file_limit: int, kind: str
) -> dict[str, tuple[str, tuple[int, ...]]]:
    inventory = artifact._inventory(root, max_files=file_limit)
    aliases: set[str] = set()
    total = 0
    for name, (node, info) in inventory.items():
        if not name:
            continue
        parts = name.split("/")
        if (
            len(parts) > 4
            or unicodedata.normalize("NFC", name) != name
            or any(re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", part) is None for part in parts)
            or name.casefold() in aliases
        ):
            raise ValueError("Unexpected or aliased diagnostic member")
        aliases.add(name.casefold())
        if node == "file":
            total += info[2]
            if (
                kind == "logs"
                and re.fullmatch(
                    r"(install|capture)/\1-(?:stdout|stderr)-[0-9]{6}\.bin",
                    name,
                )
                is None
            ):
                raise ValueError("Unexpected phase stream member")
            if kind == "data" and not data_name(name):
                raise ValueError("Unexpected diagnostic data member")
    if total > byte_limit:
        raise ValueError("Diagnostic group exceeds its fixed partition")
    return inventory


def data_name(name: str) -> bool:
    if name in {"result.json", "versions.json", "framework/index.json"}:
        return True
    if re.fullmatch(r"(?:baseline|observed)/(?:summary|source-before|source-after)\.json", name):
        return True
    if re.fullmatch(r"(?:(?:baseline|observed)/reports|observed/events)-[0-9]{6}\.jsonl", name):
        return True
    modules = {
        "textual.dom",
        "textual.widget",
        "textual.pilot",
        "textual.reactive",
        "textual.message_pump",
        "textual.await_remove",
        "textual.containers",
        "textual._arrange",
        "textual._layout",
        "textual.layout",
        "textual.scroll_view",
        "textual.timer",
        "textual.widgets._text_area",
        "rich.cells",
    }
    return name in {"framework/" + module + ".py" for module in modules}


def statuses(
    data: Path, artifact: Any, expected: list[str], binding: dict[str, Any]
) -> dict[str, Any]:
    result = json.loads(artifact._regular(data / "result.json", data))
    if (
        not isinstance(result, dict)
        or result.get("binding") != binding
        or type(result.get("schema_version")) is not int
        or result["schema_version"] != 1
        or result.get("purpose") != binding["purpose"]
        or any(result.get(name) is not False for name in AUTHORITY)
    ):
        raise ValueError("Diagnostic result binding or authority flags differ")
    summaries = {}
    complete = result.get("complete") is True and result.get("errors") == {}
    passed = complete
    pairs = {(case, phase) for case in expected for phase in ("setup", "call", "teardown")}
    for variant in ("baseline", "observed"):
        value = json.loads(artifact._regular(data / variant / "summary.json", data))
        published = result.get(variant)
        if (
            not isinstance(value, dict)
            or not isinstance(published, dict)
            or any(published.get(key) != item for key, item in value.items())
            or value.get("binding") != binding
            or value.get("variant") != variant
        ):
            raise ValueError("Diagnostic variant differs from its original summary")
        reports = value.get("reports", [])
        actual = [(row.get("nodeid"), row.get("when")) for row in reports]
        variant_complete = (
            value.get("complete") is True
            and value.get("expected_case_ids") == expected
            and value.get("collected_case_ids") == expected
            and len(actual) == len(pairs)
            and set(actual) == pairs
            and value.get("missing_phases") == []
            and value.get("errors") == {}
        )
        process = published.get("process", {})
        complete = complete and variant_complete
        passed = (
            passed
            and variant_complete
            and type(value.get("pytest_exit_code")) is int
            and value.get("pytest_exit_code") == 0
            and type(process.get("exit_code")) is int
            and process.get("exit_code") == 0
            and process.get("timed_out") is False
            and process.get("descendants_remaining") is False
            and all(row.get("outcome") == "passed" for row in reports)
        )
        summaries[variant] = value
    ruff = result.get("ruff", [])
    commands = [["check", "--output-format", "full"], ["format", "--check", "--diff"]]
    if not isinstance(ruff, list) or [row.get("arguments") for row in ruff] != commands:
        complete = passed = False
    else:
        for row in ruff:
            ran = (
                type(row.get("exit_code")) is int
                and row.get("timed_out") is False
                and row.get("descendants_remaining") is False
            )
            complete = complete and ran
            passed = passed and ran and row["exit_code"] == 0
    freeze = result.get("dependency_freeze", {})
    freeze_ok = (
        type(freeze.get("exit_code")) is int
        and freeze.get("exit_code") == 0
        and freeze.get("timed_out") is False
        and freeze.get("descendants_remaining") is False
    )
    complete = complete and freeze_ok
    passed = passed and freeze_ok
    # A baseline failure survives an observed pass and complete collection.
    return {
        "adapter_result": result,
        "variant_summaries": summaries,
        "expected_case_ids": expected,
        "diagnostic_data_complete": complete,
        "diagnostic_passed": passed,
    }


def collected(
    working: Path,
    candidate: Path,
    guard_root: Path,
    temporary: Path,
    report: dict[str, Any],
    isolation: Any,
    closure: dict[str, Any],
) -> Path:
    artifact = isolation.trusted_module("capture_artifact")
    logs = working / "logs"
    data = candidate / "lifecycle-diagnostics"
    metadata = working / "metadata"
    metadata.mkdir(mode=0o700)
    # No active writer, container or uncertain ownership may reach this function.
    write_json(metadata / "guard.json", json.loads(regular(guard_root / "guard.json", MIB)))
    write_json(metadata / "host.json", report)
    ownership = working / "ownership.json"
    if ownership.exists():
        write_json(metadata / "ownership.json", json.loads(regular(ownership, MIB)))
    groups = [(logs, "logs", LOG_LIMIT, 16), (metadata, "metadata", META_LIMIT - 512 * 1024, 15)]
    if data.exists() or data.is_symlink():
        groups.append((data, "data", DATA_LIMIT, 224))
    else:
        report["ok"] = False
        report["data_missing"] = True
    root = temporary / ("atk-lifecycle-artifact-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    copied = []
    collection_errors = []
    for source, name, capacity, count in groups:
        try:
            bounded_inventory(source, artifact, byte_limit=capacity, file_limit=count, kind=name)
            isolation.collect(source, root / name, artifact)
            inventory = bounded_inventory(
                root / name, artifact, byte_limit=capacity, file_limit=count, kind=name
            )
            for path, (node, _info) in inventory.items():
                if node == "file":
                    body = artifact._regular(root / name / path, root / name)
                    copied.append(
                        {"path": name + "/" + path, "bytes": len(body), "sha256": digest(body)}
                    )
        except (Exception, KeyboardInterrupt) as exc:
            collection_errors.append(name + ": " + type(exc).__name__ + ": " + str(exc))
            # A refused group may have a partial publication. It is host-created,
            # unreachable by candidates, and must not leak unindexed bytes.
            if (root / name).exists():
                shutil.rmtree(root / name)
            copied = [row for row in copied if not row["path"].startswith(name + "/")]
    outcome = {}
    try:
        binding = {
            **report["binding"],
            "public_context_sha256": closure["public_context_sha256"],
            "adapter_sha256": closure["adapter_sha256"],
        }
        outcome = statuses(root / "data", artifact, closure["expected_case_ids"], binding)
    except (Exception, KeyboardInterrupt) as exc:
        collection_errors.append("Missing diagnostic status: " + str(exc))
    manifest = {
        **report,
        **outcome,
        "copied": copied,
        "collection_errors": collection_errors,
        "collection_complete": not collection_errors
        and not report.get("data_missing", False)
        and outcome.get("diagnostic_data_complete") is True,
        "job_id": None,
        "artifact_id": None,
        "final_job_conclusion": None,
        "final_upload_conclusion": None,
        **AUTHORITY,
    }
    manifest["ok"] = (
        report["ok"]
        and manifest["collection_complete"]
        and outcome.get("diagnostic_passed") is True
    )
    write_json(root / "manifest.json", manifest, 512 * 1024)
    final = artifact._inventory(root)
    actual_files = {name for name, (node, _info) in final.items() if node == "file"}
    if actual_files != {row["path"] for row in copied} | {"manifest.json"}:
        raise ValueError("Final diagnostic membership differs from its complete index")
    if sum(info[2] for node, info in final.values() if node == "file") > 100 * MIB:
        raise ValueError("Joint diagnostic artifact exceeds fixed capacity")
    report["collection_complete"] = manifest["collection_complete"]
    report["ok"] = manifest["ok"]
    return root


def run(environment: dict[str, str]) -> bool:
    binding = identity(environment)
    control = directory(Path(environment["DIAGNOSTIC_CONTROL_ROOT"]))
    subject = directory(Path(environment["DIAGNOSTIC_SUBJECT_ROOT"]))
    temporary = directory(Path(environment["RUNNER_TEMP"]))
    guard_root = directory(Path(environment["DIAGNOSTIC_METADATA_ROOT"]))
    separate(control, subject, temporary)
    if (
        guard_root.parent != temporary
        or re.fullmatch(r"atk-lifecycle-guard-[0-9a-f]{32}", guard_root.name) is None
    ):
        raise ValueError("Guard metadata is outside its owned root")
    original_guard = regular(guard_root / "guard.json", MIB)
    guard_value = json.loads(original_guard)
    if guard_value.get("ok") is not True or guard_value.get("binding") != binding:
        raise ValueError("Guard metadata differs from this exact invocation")
    if (
        git_head(control, environment) != binding["workflow_sha"]
        or git_head(subject, environment) != SUBJECT
    ):
        raise ValueError("Source checkouts differ from the fixed experiment")
    isolation, closure = source_closure(control)
    if guard_value["closure"] != closure:
        raise ValueError("Source closure changed after guard")
    for row in closure["subject_files"]:
        if digest(regular(subject / row["path"], MIB)) != row["sha256"]:
            raise ValueError("Fixed baseline source differs")
    working = temporary / ("atk-lifecycle-private-" + uuid.uuid4().hex)
    working.mkdir(mode=0o700)
    candidate = working / "subject"
    copy_subject(subject, candidate)
    logs = working / "logs"
    logs.mkdir(mode=0o700)
    sandbox_module = isolation.trusted_module("capture_sandbox")
    sandbox = sandbox_module.Sandbox(
        candidate, control, working / "ownership.json", python_version=binding["python_line"]
    )
    report: dict[str, Any] = {
        "binding": binding,
        "closure": closure,
        "phases": [],
        "ok": False,
        **AUTHORITY,
    }
    try:
        sandbox.setup()
        report["image"] = sandbox.image
        descriptor = {
            **binding,
            "public_context_sha256": closure["public_context_sha256"],
            "adapter_sha256": closure["adapter_sha256"],
        }
        encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > 4096:
            raise ValueError("Closed diagnostic binding exceeds capacity")
        install = (
            "python -m venv /work/.venv && /work/.venv/bin/python -m pip install "
            "-e '.[tui]' -r agent_tools/requirements-mcp.txt pytest ruff==0.15.22 '"
            + PROFILES[binding["python_line"]]
            + "'"
        )
        commands = [
            ("install", ["sh", "-ec", install], 600),
            (
                "capture",
                [
                    "/work/.venv/bin/python",
                    "-I",
                    "/trusted/.github/agent/lifecycle_diagnostic_pytest.py",
                    "--binding",
                    encoded,
                ],
                900,
            ),
        ]
        for phase, command, timeout in commands:
            container = sandbox.create(phase, command, binding)
            sandbox.verify()
            sandbox.validate_container(container)
            destination = logs / phase
            destination.mkdir(mode=0o700)
            retained = sum(
                item["bytes"]
                for result in report["phases"]
                for stream in result["streams"].values()
                for item in stream["parts"]
            )
            result = isolation.contained_output(
                container,
                phase,
                destination,
                timeout,
                limit=LOG_LIMIT - retained,
                quarantine_root=working,
            )
            report["phases"].append(result)
            sandbox.remove(container)
            if not result["ok"]:
                break
        report["ok"] = len(report["phases"]) == 2 and all(p["ok"] for p in report["phases"])
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        signal.alarm(180)
        try:
            sandbox.cleanup()
            report["cleanup_complete"] = True
        except (Exception, KeyboardInterrupt) as exc:
            report["cleanup_complete"] = False
            report["cleanup_error"] = type(exc).__name__ + ": " + str(exc)
            report["ok"] = False
        report["remaining_containers"] = list(sandbox.containers)
        report["remaining_network"] = sandbox.network_id
        report["remaining_guards"] = list(sandbox.rules)
        report["uncertain_operations"] = sandbox.uncertain_operations()
        signal.alarm(120)
    safe = (
        report["cleanup_complete"]
        and not sandbox.containers
        and not sandbox.network_id
        and not sandbox.rules
        and not sandbox.uncertain_operations()
        and not isolation.UNSAFE_LOG_WRITER.is_set()
        and all(
            p["readers_stopped"] and p["logs_safe"] and not p["quarantined_logs"]
            for p in report["phases"]
        )
    )
    if not safe:
        return False  # Always-upload can see only the earlier closed guard root.
    if regular(guard_root / "guard.json", MIB) != original_guard:
        raise ValueError("Immutable guard metadata changed")
    output = collected(working, candidate, guard_root, temporary, report, isolation, closure)
    emit(environment, "artifact_root", output)
    return report["ok"] is True


def main() -> None:
    if sys.argv[1:] not in ([], ["validate"]):
        raise SystemExit("Only the fixed diagnostic or validate entrypoint is supported")

    def interrupted(number: int, _frame: Any) -> None:
        if number == signal.SIGALRM:
            raise TimeoutError("Diagnostic wall-clock budget expired")
        raise InterruptedError("Diagnostic interrupted; owned cleanup required")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGALRM, interrupted)
    signal.alarm(26 * 60)
    try:
        ok = guard(dict(os.environ)) if sys.argv[1:] else run(dict(os.environ))
    finally:
        signal.alarm(0)
    if not ok:
        raise SystemExit(
            "Fixed lifecycle diagnostic failed; inspect original outcomes and completeness"
        )


if __name__ == "__main__":
    main()
