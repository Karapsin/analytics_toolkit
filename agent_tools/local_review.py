"""Fresh local read-only review before the one-way feature publication."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from threading import RLock
from typing import Any

from agent_tools import (
    agent_runtimes,
    bootstrap_process,
    review_checkpoints,
    review_chunks,
    review_iterations,
    sessions,
    sql_explorer_visual,
    task_context,
)

RUNTIME_ADAPTER = "agent_tools/agent_runtimes.py"
# Built-in Codex reviewer contract, used when the dev base predates the shared
# runtime adapter. tests/repository/agent_runtime_parity.py keeps these values
# byte-identical to agent_tools/agent_runtimes.py.
REVIEW_TIMEOUT = agent_runtimes.REVIEW_TIMEOUT
ENV_DENY = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "SSH_AUTH_SOCK",
        "GIT_ASKPASS",
    }
)
REVIEW_PROMPT = agent_runtimes.REVIEW_PROMPT
AUTHOR_SESSION_ENV = ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID")
VISUAL_EVIDENCE_KEY = "sql_explorer_visual_review"
VISUAL_ORDER_KEY = "visual_review_order"
_PROVENANCE_LOCK = RLock()
_PROTOCOL_MODULES: dict[tuple[str, str], Any] = {}


def protocol(root: Path) -> Any:
    data = trusted_source(root, sessions.load(root)["base"], ".github/agent/local_review.py")
    digest = hashlib.sha256(data).hexdigest()
    identity = (str(root.resolve()), digest)
    with _PROVENANCE_LOCK:
        if identity not in _PROTOCOL_MODULES:
            path = directory(root) / ("protocol-" + digest + ".py")
            if path.is_symlink():
                msg = "Unsafe local review protocol path"
                raise ValueError(msg)
            path.write_bytes(data)
            spec = importlib.util.spec_from_file_location("atk_local_review_protocol", path)
            if spec is None or spec.loader is None:
                msg = "Local review protocol cannot be loaded"
                raise RuntimeError(msg)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _PROTOCOL_MODULES[identity] = module
        return _PROTOCOL_MODULES[identity]


def binding(root: Path, head: str) -> dict[str, str]:
    context = task_context.read(root)
    proto = task_context.protocol()
    message = sessions.run(root, "git", "log", "-1", "--format=%B", head)
    return {
        "repository": task_context.repository_identity(root),
        "head": head,
        "base": sessions.load(root)["base"],
        "tree": sessions.run(root, "git", "rev-parse", head + "^{tree}"),
        "task_digest": proto.digest(proto.public_context(context)),
        "producer_id": proto.verify_commit(proto.public_context(context), message),
    }


def _discovery_binding(root: Path, head: str) -> dict[str, str]:
    """Bind diagnostic source to the authenticated current task without approving it."""
    context = task_context.protocol().public_context(task_context.read(root))
    message = sessions.run(root, "git", "log", "-1", "--format=%B", head)
    actors = re.findall(r"^Agent-Producer: ([A-Za-z0-9:-]{1,160})$", message, re.MULTILINE)
    if actors != [context["producer_id"]]:
        msg = "Discovery requires an immutable candidate from the active task producer"
        raise ValueError(msg)
    return {
        "repository": task_context.repository_identity(root),
        "head": head,
        "base": sessions.load(root)["base"],
        "tree": sessions.run(root, "git", "rev-parse", head + "^{tree}"),
        "task_digest": task_context.protocol().digest(context),
        "producer_id": actors[0],
    }


def _discovery_evidence(root: Path) -> dict[str, Any]:
    """Original task/plan/images are supplied separately; private repair NAV stays private."""
    del root
    return {}


def _review_inputs(supplied: dict[str, Any]) -> dict[str, Any]:
    """Forward only independently validated visual/order evidence, never author repair hints."""
    return {
        key: supplied[key] for key in (VISUAL_EVIDENCE_KEY, VISUAL_ORDER_KEY) if key in supplied
    }


def directory(root: Path) -> Path:
    path = sessions.state_path(root).with_name("atk-local-reviews")
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def verified(root: Path, head: str) -> dict[str, Any]:
    proto = protocol(root)
    path = directory(root) / (head + ".json")
    if path.is_symlink() or not path.is_file():
        msg = "Fresh independent local review is required before publication."
        raise RuntimeError(msg)
    receipt = json.loads(path.read_text())
    proto.validate_binding(receipt, **binding(root, head))
    verify_signature(root, receipt)
    review_iterations.validate_convergence(root, receipt["payload"])
    return receipt


def verify_signature(root: Path, receipt: dict[str, Any]) -> None:
    """Verify local provenance without implying converged source approval."""
    with _PROVENANCE_LOCK:
        key = Path(sessions.load(root)["signing_key"]).with_suffix(".pub")
        allowed = directory(root) / "allowed-signers"
        if allowed.is_symlink():
            msg = "Unsafe local reviewer enrollment path"
            raise ValueError(msg)
        allowed.write_text("atk-local " + key.read_text().strip() + "\n")
        allowed.chmod(0o600)
        protocol(root).verify_signature(receipt, allowed)


def arguments(root: Path, job: Path, subject: dict[str, str]) -> list[str]:
    executable = shutil.which("codex-real") or shutil.which("codex")
    if not executable:
        msg = "Codex CLI is required for independent local review."
        raise RuntimeError(msg)
    source, schema = trusted_runtime(root, job, subject["base"])
    args = [
        executable,
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C",
        str(job),
        "-s",
        "read-only",
        "-c",
        'approval_policy="never"',
        "-c",
        'forced_login_method="chatgpt"',
        "-c",
        "hooks={}",
        "-c",
        'web_search="disabled"',
    ]
    for feature in (
        "shell_tool",
        "unified_exec",
        "shell_snapshot",
        "plugins",
        "apps",
        "skill_mcp_dependency_install",
    ):
        args.extend(["--disable", feature])
    server = provider(source, root, subject, job)
    args.extend(
        [
            "-c",
            "mcp_servers.source_files.command=" + json.dumps(server[0]),
            "-c",
            "mcp_servers.source_files.args=" + json.dumps(server[1:]),
            "-c",
            "mcp_servers.source_files.required=true",
            "-c",
            'mcp_servers.source_files.default_tools_approval_mode="approve"',
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(job / "verdict.json"),
            "--json",
            "-",
        ]
    )
    return agent_runtimes.with_source_review_effort("codex", args)


def provider(source: Path, root: Path, subject: dict[str, str], job: Path) -> list[str]:
    """Trusted immutable source server command shared by every reviewer runtime."""
    return [
        sys.executable,
        "-I",
        str(source),
        "--root",
        str(root),
        "--head",
        subject["head"],
        "--base",
        subject["base"],
        "--ledger",
        str(job / "served.jsonl"),
    ]


def select_reviewer(root: Path) -> str:
    """ATK_LOCAL_REVIEWER > session reviewer > session runtime > CLI detection > codex."""
    state = sessions.load(root)
    for value in (
        os.environ.get("ATK_LOCAL_REVIEWER"),
        state.get("reviewer"),
        state.get("runtime"),
    ):
        if value:
            if value not in {"codex", "claude"}:
                msg = f"Unknown local reviewer runtime: {value}"
                raise RuntimeError(msg)
            return str(value)
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "claude"
    return "codex"


def runtime_adapter(root: Path, job: Path, base: str) -> Any:
    """Load the reviewed runtime adapter from dev objects; absent means built-in Codex only."""
    present = subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-c",
            "core.hooksPath=/dev/null",
            "cat-file",
            "-e",
            base + ":" + RUNTIME_ADAPTER,
        ],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if present.returncode:
        manifest = (
            sessions.state_path(root).with_name("atk-local-review-bootstrap") / "manifest.json"
        )
        if not manifest.is_file() or RUNTIME_ADAPTER not in json.loads(manifest.read_text()).get(
            "files", {}
        ):
            return None
    data = trusted_source(root, base, RUNTIME_ADAPTER)
    digest = hashlib.sha256(data).hexdigest()
    path = job / ("agent_runtimes-" + digest + ".py")
    path.write_bytes(data)
    name = "atk_trusted_agent_runtimes_" + digest + "_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        msg = "Reviewed runtime adapter cannot be loaded"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _cli_version(executable: str, environment: dict[str, str]) -> str:
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    lines = result.stdout.strip().splitlines()
    return lines[0][:200] if result.returncode == 0 and lines else "unknown"


def _codex_events(events: bytes, job: Path) -> dict[str, Any]:
    threads = [
        event["thread_id"]
        for line in events.decode().splitlines()
        if (event := json.loads(line)).get("type") == "thread.started"
    ]
    if len(threads) != 1:
        msg = "A separate fresh reviewer session is required."
        raise RuntimeError(msg)
    return {
        "session_id": threads[0],
        "verdict": json.loads((job / "verdict.json").read_text()),
        "model": "codex-default",
        "version": None,
        "auth": "chatgpt",
    }


def _accepted_verdict(observed: dict[str, Any], subject: dict[str, str]) -> dict[str, Any]:
    authors = {os.environ.get(name) for name in AUTHOR_SESSION_ENV} | {subject["producer_id"]}
    if observed["session_id"] in authors - {None, ""}:
        msg = "A separate fresh reviewer session is required."
        raise RuntimeError(msg)
    verdict = observed["verdict"]
    if not _complete_verdict(verdict):
        summary = verdict.get("summary") if isinstance(verdict, dict) else None
        msg = (
            "Local review rejected; correct locally before publication: "
            + str(summary or "Incomplete verdict")[:2000]
        )
        raise RuntimeError(msg)
    return verdict


def _complete_verdict(verdict: object) -> bool:
    """Fail closed on anything except an explicit, complete, aligned approval."""
    return (
        isinstance(verdict, dict)
        and verdict.get("approved") is True
        and verdict.get("alignment") == "aligned"
        and isinstance(verdict.get("summary"), str)
        and bool(verdict["summary"].strip())
        and isinstance(verdict.get("change_summary"), str)
        and isinstance(verdict.get("findings"), list)
    )


def _public_verdict(verdict: Any) -> Any:
    """Sanitize signed transport values while preserving raw private CLI evidence."""
    redact = task_context.protocol().redact

    def sanitize(value: Any) -> Any:
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        if isinstance(value, dict):
            return {
                redact(key) if isinstance(key, str) else key: sanitize(item)
                for key, item in value.items()
            }
        return value

    return sanitize(verdict)


def trusted_runtime(root: Path, job: Path, base: str) -> tuple[Path, Path]:
    """Load reviewed tools from dev objects, never executable candidate content."""
    names = ("agent_tools/local_review_files.py", ".github/agent/review.schema.json")
    targets = job / "source_provider.py", job / "review.schema.json"
    for target, name in zip(targets, names):
        target.write_bytes(trusted_source(root, base, name))
    return targets


def trusted_source(root: Path, base: str, name: str) -> bytes:
    result = subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-c",
            "core.hooksPath=/dev/null",
            "show",
            base + ":" + name,
        ],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        bootstrap = sessions.state_path(root).with_name("atk-local-review-bootstrap")
        manifest = json.loads((bootstrap / "manifest.json").read_text())
        if not manifest.get("reviewer_id"):
            msg = "Independent bootstrap reviewer identity is required"
            raise ValueError(msg)
        data = (bootstrap / Path(name).name).read_bytes()
        if hashlib.sha256(data).hexdigest() != manifest["files"][name]:
            msg = "Trusted local reviewer bootstrap digest mismatch"
            raise ValueError(msg)
        return data
    return result.stdout


def validation(root: Path) -> dict[str, Any]:
    from agent_tools import mcp_server  # noqa: PLC0415 - precommit/reviewer dependency cycle.

    result = mcp_server._verify_precommit_success(root)  # noqa: SLF001 - shared exact-tree receipt.
    if not result["ok"]:
        raise RuntimeError("Exact-tree mandatory validation is required: " + result["message"])
    return {
        "verification": result,
        "receipt": json.loads((root / mcp_server.CHECK_STATE_FILE).read_text()),
    }


def review(root: Path) -> dict[str, Any]:
    """Serialize the complete task review; process death releases the OS lock."""
    try:
        with bootstrap_process.lock(directory(root) / "iterations.lock", wait=False):
            return _review(root)
    except BlockingIOError as exc:
        msg = "An independent review of this task is already running"
        raise RuntimeError(msg) from exc


def discover(root: Path) -> dict[str, Any]:
    """Run nonapproving discovery on committed HEAD, allowing unrelated dirty work."""
    try:
        with bootstrap_process.lock(directory(root) / "iterations.lock", wait=False):
            return _review(root, discovery_only=True)
    except BlockingIOError as exc:
        msg = "An independent review of this task is already running"
        raise RuntimeError(msg) from exc


def plan_review(root: Path, plan_file: Path) -> dict[str, Any]:
    """Install the implementor's bounded assignments for immutable committed source."""
    try:
        with bootstrap_process.lock(directory(root) / "iterations.lock", wait=False):
            return _install_review_plan(root, plan_file)
    except BlockingIOError as exc:
        msg = "Cannot replace review assignments while a review wave is running"
        raise RuntimeError(msg) from exc


def _install_review_plan(root: Path, plan_file: Path) -> dict[str, Any]:
    sessions.writable(root)
    head = sessions.run(root, "git", "rev-parse", "HEAD")
    subject = _discovery_binding(root, head)
    path = plan_file.expanduser()
    supplied = json.loads(path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="atk-review-plan-") as temporary:
        source, _ = trusted_runtime(root, Path(temporary), subject["base"])
        module = review_chunks.load_provider(source)
        plan = review_chunks.install(root, subject, module, supplied)
    chunks = plan["chunks"]
    return {
        "summary": "Bounded parallel review plan installed; source is not approved.",
        "head": head,
        "plan_sha256": review_chunks.plan_digest(plan),
        "chunk_count": len(chunks),
        "component_chunk_count": sum(chunk["kind"] == "component" for chunk in chunks),
        "integration_chunk_count": sum(chunk["kind"] == "integration" for chunk in chunks),
        "max_workers": plan["limits"]["max_workers"],
        "approval_eligible": False,
    }


def _cached_plan_changed(root: Path, subject: dict[str, str], target: Path) -> bool:
    """Archive an authentic old assignment approval before reviewing a replacement plan."""
    if target.is_symlink():
        return False
    try:
        receipt = json.loads(target.read_text())
        cached = receipt["payload"]["review_iterations"]["plan_sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not _sha256_hex(cached) or cached == review_chunks.installed_digest(root, subject):
        return False
    protocol(root).validate_binding(receipt, **subject)
    verify_signature(root, receipt)
    return True


def _cached_approval(
    root: Path, subject: dict[str, str], target: Path, *, discovery_only: bool
) -> dict[str, Any] | None:
    if not target.exists() or discovery_only:
        return None
    recorded = _receipt_base(target)
    if recorded is None or recorded == subject["base"]:
        if not _cached_plan_changed(root, subject, target):
            return verified(root, subject["head"])
        suffix = ".superseded-plan-"
    else:
        suffix = ".superseded-"
    # Retain the old proof; a new base or assignment needs fresh scoped results.
    target.replace(target.with_name(subject["head"] + suffix + uuid.uuid4().hex[:8] + ".json"))
    return None


def _review_request(
    root: Path, subject: dict[str, str], checks: dict[str, Any], *, discovery_only: bool
) -> tuple[dict[str, Any], Any]:
    supplied = _review_inputs(
        _discovery_evidence(root) if discovery_only else review_evidence(root, subject)
    )
    context = task_context.protocol().public_context(task_context.read(root))
    request = {
        "subject": subject,
        "discovery_only": discovery_only,
        "user_task": context,
        "validation": checks,
        "evidence": supplied,
    }
    with tempfile.TemporaryDirectory(prefix="atk-review-inventory-") as temporary:
        source, _ = trusted_runtime(root, Path(temporary), subject["base"])
        module = review_chunks.load_provider(source)
        request["review_plan"] = review_chunks.load(root, subject, module)
    if discovery_only:
        _qualify_discovery_request(root, subject, request)
    return request, module


def _qualify_discovery_request(
    root: Path, subject: dict[str, str], request: dict[str, Any]
) -> None:
    message = sessions.run(root, "git", "log", "-1", "--format=%B", subject["head"])
    attached = re.findall(r"^Agent-Task-Evidence: ([0-9a-f]{64})$", message, re.MULTILINE)
    if not attached:
        attached = re.findall(r"^Agent-Task-Context: ([0-9a-f]{64})$", message, re.MULTILINE)
    if len(attached) != 1:
        msg = "Discovery requires the committed candidate's historical task attachment"
        raise ValueError(msg)
    request["candidate_task_attachment"] = {
        "digest": attached[0],
        "matches_current_task": attached[0] == subject["task_digest"],
        "approval_valid": False,
    }
    request["qualification"] = (
        "Diagnostic immutable Git source discovery only. Dirty worktree content is excluded. "
        "Current authenticated task corrections apply. Committed task attachments and "
        "checks may be historical; private author repair history is not supplied. "
        "Never issue final approval, run checks, "
        "repair source, publish or start visuals from this action."
    )


def _review(root: Path, *, discovery_only: bool = False) -> dict[str, Any]:
    """Leave rejected commits local; each new candidate needs another fresh reviewer."""
    sessions.writable(root)
    if not discovery_only:
        sessions.clean(root)
        # Advance only normal approval's recorded base; diagnostic discovery is frozen.
        sessions.advance_base(root)
    head = sessions.run(root, "git", "rev-parse", "HEAD")
    subject = _discovery_binding(root, head) if discovery_only else binding(root, head)
    checks = (
        {"status": "historical_or_unavailable", "approval_valid": False, "checks_not_run": True}
        if discovery_only
        else validation(root)
    )
    target = directory(root) / (head + ".json")
    cached = _cached_approval(root, subject, target, discovery_only=discovery_only)
    if cached is not None:
        return cached
    request, module = _review_request(root, subject, checks, discovery_only=discovery_only)

    # Keep the CLI outside every candidate/project configuration directory.
    def execute(round_request: str) -> dict[str, Any]:
        retained = directory(root) / ("run-" + uuid.uuid4().hex)
        with tempfile.TemporaryDirectory(prefix="atk-independent-review-") as temporary:
            job = Path(temporary)
            try:
                return _execute_review(root, job, subject, round_request)
            finally:
                shutil.copytree(job, retained)

    def sign_wave(payload: dict[str, Any]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="atk-review-wave-") as temporary:
            return _sign_payload(root, Path(temporary), payload)

    def verify_wave(manifest: dict[str, Any], ledgers: dict[str, Path]) -> None:
        review_chunks.aggregate_coverage(module, root, subject, manifest, ledgers)

    if discovery_only:
        batch = review_iterations.discover(
            root, subject, request, execute, sign_wave=sign_wave, verify_wave=verify_wave
        )
        if (
            subject != _discovery_binding(root, head)
            or sessions.run(root, "git", "rev-parse", "HEAD") != head
        ):
            msg = "Candidate or task changed during immutable discovery"
            raise RuntimeError(msg)
        return {**batch, "head": head, "base": subject["base"], "approval_eligible": False}
    rounds = review_iterations.run(
        root, subject, request, execute, sign_wave=sign_wave, verify_wave=verify_wave
    )
    if subject != binding(root, head) or sessions.run(root, "git", "rev-parse", "HEAD") != head:
        msg = "Candidate or task changed during independent review iterations"
        raise RuntimeError(msg)
    sessions.clean(root)
    final = rounds[-1]["payload"]
    payload = {
        "version": 1,
        "kind": "local-source-review",
        "mode": "read-only",
        **subject,
        **{k: v for k, v in final.items() if k.startswith("reviewer_")},
        "approved": True,
        "alignment": "aligned",
        "summary": final["verdict"]["summary"],
        "execution_sha256": final["execution_sha256"],
        "review_iterations": review_iterations.compact_proof(root, rounds, subject),
    }
    review_iterations.validate_convergence(root, payload)
    with tempfile.TemporaryDirectory(prefix="atk-review-convergence-") as temporary:
        receipt = _sign_payload(root, Path(temporary), payload)
    temporary_target = target.with_suffix(".tmp")
    temporary_target.write_bytes(protocol(root).canonical(receipt))
    temporary_target.chmod(0o600)
    temporary_target.replace(target)
    return verified(root, head)


def _reviewer_command(
    root: Path, job: Path, subject: dict[str, str], session_id: str
) -> tuple[str, Any, list[str], dict[str, str]]:
    """Select the reviewer CLI and build its isolated invocation; never substitute another."""
    adapter = runtime_adapter(root, job, subject["base"])
    if adapter is None:
        runtime = select_reviewer(root)
        if runtime != "codex":
            msg = (
                f"The {runtime} local reviewer requires the reviewed runtime adapter in the "
                "dev base; no other reviewer is substituted."
            )
            raise RuntimeError(msg)
        environment = {key: value for key, value in os.environ.items() if key not in ENV_DENY}
        command = agent_runtimes.with_source_review_effort(runtime, arguments(root, job, subject))
        return runtime, None, command, environment
    runtime = adapter.select_reviewer(os.environ, sessions.load(root))
    source, schema = trusted_runtime(root, job, subject["base"])
    spec = adapter.RUNTIMES[runtime]
    command = spec.reviewer_argv(
        adapter.reviewer_binary(runtime),
        job,
        provider(source, root, subject, job),
        schema,
        session_id,
    )
    # The dev-base adapter remains authoritative for isolation and source access.
    # Its older versions may omit this fixed, performance-only Codex setting.
    command = agent_runtimes.with_source_review_effort(runtime, command)
    missing = adapter.policy_violations(runtime, command)
    if missing:
        msg = "Reviewer isolation policy is incomplete: " + ", ".join(missing)
        raise RuntimeError(msg)
    return runtime, adapter, command, spec.reviewer_env(os.environ)


def _execute_review(root: Path, job: Path, subject: dict[str, str], request: str) -> dict[str, Any]:
    head = subject["head"]
    requested = json.loads(request)
    discovery_only = requested.get("discovery_only") is True
    session_id = str(uuid.uuid4())
    runtime, adapter, command, environment = _reviewer_command(root, job, subject, session_id)
    source, _ = trusted_runtime(root, job, subject["base"])
    visible = json.dumps(review_checkpoints.worker_input(requested), ensure_ascii=False)
    prompt = (REVIEW_PROMPT if adapter is None else adapter.REVIEW_PROMPT) + visible
    timeout = REVIEW_TIMEOUT if adapter is None else max(REVIEW_TIMEOUT, adapter.REVIEW_TIMEOUT)
    version = _cli_version(command[0], environment)
    with (job / "events.jsonl").open("w") as output, (job / "stderr.log").open("w") as errors:
        result = subprocess.run(
            command,
            input=prompt,
            text=True,
            stdout=output,
            stderr=errors,
            env=environment,
            cwd=job,
            timeout=timeout,
            check=False,
        )
    if result.returncode:
        msg = "Local reviewer did not complete; private logs and source are retained."
        raise RuntimeError(msg)
    current = _discovery_binding(root, head) if discovery_only else binding(root, head)
    if subject != current or sessions.run(root, "git", "rev-parse", "HEAD") != head:
        msg = "Candidate or task changed during local review."
        raise RuntimeError(msg)
    if not discovery_only:
        sessions.clean(root)
    events = (job / "events.jsonl").read_bytes()
    if adapter is None:
        observed = _codex_events(events, job)
    else:
        observed = adapter.RUNTIMES[runtime].parse_events(events, job, session_id)
    authors = {os.environ.get(name) for name in AUTHOR_SESSION_ENV} | {subject["producer_id"]}
    if observed["session_id"] in authors - {None, ""}:
        msg = "A separate fresh reviewer session is required."
        raise RuntimeError(msg)
    if "review_chunk" in requested:
        coverage = review_checkpoints.certify(
            root, subject, requested, job, source, directory(root)
        )
    else:
        checked = subprocess.run(
            [*provider(source, root, subject, job), "--verify-ledger"],
            capture_output=True,
            check=False,
        )
        coverage = {"coverage_complete": checked.returncode == 0}
    iteration = requested["iteration"]
    payload = {
        "version": 1,
        "kind": "local-review-round",
        **subject,
        "reviewer_id": "reviewer:" + observed["session_id"],
        "reviewer_runtime": (
            "codex" if adapter is None else adapter.RUNTIMES[runtime].receipt_name
        ),
        "reviewer_version": observed.get("version") or version,
        "reviewer_model": observed["model"],
        "reviewer_auth": observed["auth"],
        "verdict": _public_verdict(observed["verdict"]),
        **coverage,
        "prior_findings_sha256": review_iterations.findings_digest(iteration["prior_findings"]),
        "previous_rounds_sha256": iteration["previous_rounds_sha256"],
        "review_procedure": review_iterations.PROCEDURE,
        "discovery_only": discovery_only,
        "recorded_findings_sha256": review_iterations.findings_digest(
            iteration["recorded_findings"]
        ),
        "execution_sha256": hashlib.sha256(events).hexdigest(),
    }
    receipt = _sign_payload(root, job, payload)
    # Parallel workers own only their scope receipt. The coordinator serializes
    # durable child outcomes and the aggregate wave in the shared task journal.
    if "review_chunk" not in requested:
        review_iterations.record(root, iteration["attempt_id"], receipt)
    return receipt


def _sign_payload(root: Path, job: Path, payload: dict[str, Any]) -> dict[str, Any]:
    with _PROVENANCE_LOCK:
        proto = protocol(root)
        source = job / "receipt.json"
        source.write_bytes(proto.canonical(payload))
        subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "sign",
                "-f",
                sessions.load(root)["signing_key"],
                "-n",
                proto.NAMESPACE,
                str(source),
            ],
            capture_output=True,
            check=True,
        )
        return {"payload": payload, "signature": source.with_suffix(".json.sig").read_text()}


def evidence(root: Path, digest: str) -> dict[str, Any]:
    path = sessions.state_path(root).with_name("atk-local-review-evidence.json")
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_size > 128 * 1024:
        msg = "Private review evidence is unsafe or oversized"
        raise ValueError(msg)
    value = json.loads(path.read_text())
    if value.get("task_digest") != digest:
        msg = "Private review evidence belongs to a different task"
        raise ValueError(msg)
    return value


def _receipt_base(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return json.loads(path.read_text())["payload"]["base"]
    except (ValueError, KeyError, TypeError):
        return None


def review_evidence(root: Path, subject: dict[str, str]) -> dict[str, Any]:
    """Private evidence plus the verified SQL Explorer visual receipt when required."""
    # Validate task ownership without forwarding author navigation or fix history.
    evidence(root, subject["task_digest"])
    order = visual_review_order(root, subject["task_digest"])
    if order is not None:
        return {VISUAL_ORDER_KEY: order}
    visual = visual_evidence(root, subject)
    if visual is None:
        return {}
    return {VISUAL_EVIDENCE_KEY: visual}


def visual_review_order(root: Path, digest: str | None = None) -> dict[str, Any] | None:
    """Validate a task-bound user decision about sequencing, never waive visuals."""
    context = task_context.read(root)
    current = task_context.protocol().digest(task_context.protocol().public_context(context))
    if digest is not None and digest != current:
        msg = "Visual review order belongs to a different task"
        raise ValueError(msg)
    decision = evidence(root, current).get(VISUAL_ORDER_KEY)
    if decision is None:
        recorded = [
            (index, prompt)
            for index, prompt in enumerate(context["corrections"])
            if " ".join(prompt.split()).casefold().rstrip(".!?")
            == "do visual checks only after final review approval"
        ]
        if not recorded:
            return None
        index, prompt = recorded[-1]
        decision = {
            "decision": "source-review-before-visuals",
            "message_source": "user",
            "correction_index": index,
            "user_prompt": prompt,
        }
    fields = {"decision", "message_source", "correction_index", "user_prompt"}
    valid = isinstance(decision, dict) and set(decision) == fields
    if valid:
        index = decision["correction_index"]
        prompt = decision["user_prompt"]
        valid = (
            decision["decision"] == "source-review-before-visuals"
            and decision["message_source"] == "user"
            and type(index) is int
            and 0 <= index < len(context["corrections"])
            and prompt == context["corrections"][index]
            and isinstance(prompt, str)
            and " ".join(prompt.split()).casefold().rstrip(".!?")
            == "do visual checks only after final review approval"
        )
    if not valid:
        msg = "Visual review order requires the exact recorded user correction and decision"
        raise ValueError(msg)
    return {**decision, "task_digest": current, "visual_review_required_before_publication": True}


def visual_evidence(root: Path, subject: dict[str, str]) -> dict[str, Any] | None:
    """Attach the completed visual receipt only for changes that require it.

    ``review`` requires a clean checkout, so the receipt's working-tree content
    fingerprint is the exact candidate tree. Incomplete, stale or unreadable
    receipts fail before the reviewer starts.
    """
    # NUL-separated names avoid core.quotePath quoting of non-ASCII paths.
    changed = sessions.run(
        root, "git", "diff", "--name-only", "-z", "--no-renames", subject["base"], subject["head"]
    ).split("\0")
    visual_paths = sorted(
        path for path in changed if sql_explorer_visual.visual_review_required([path])
    )
    if not visual_paths:
        return None
    sessions.clean(root)
    path = root / sql_explorer_visual.RECEIPT
    if path.is_symlink():
        msg = "SQL Explorer visual-review receipt is unsafe"
        raise ValueError(msg)
    status = sql_explorer_visual.verify_visual_receipt(root, paths=visual_paths)
    if not status["ok"]:
        msg = "Completed SQL Explorer visual review is required before local review: "
        raise RuntimeError(msg + status["message"])
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != status["receipt_sha256"]:
        msg = "SQL Explorer visual-review receipt changed during verification"
        raise RuntimeError(msg)
    _completed_visual_receipt(root, json.loads(data))
    return {
        "kind": "sql-explorer-visual-review",
        "verified": True,
        "task_digest": subject["task_digest"],
        "head": subject["head"],
        "tree": subject["tree"],
        "base": subject["base"],
        "changed_visual_paths": visual_paths,
        "receipt_sha256": status["receipt_sha256"],
        "receipt": json.loads(data),
    }


def _sha256_hex(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64  # noqa: PLR2004 - SHA-256 hex digest length
        and all(char in "0123456789abcdef" for char in value)
    )


def _completed_visual_receipt(root: Path, receipt: dict[str, Any]) -> None:
    """Require a receipt written by ``complete_review`` and its completed session record."""
    incomplete = "SQL Explorer visual-review receipt is not a completed visual review: "
    scenes = receipt.get("scenes")
    review_id = receipt.get("review_id")
    if (
        receipt.get("schema_version") != 1
        or not isinstance(review_id, str)
        or not isinstance(receipt.get("host"), dict)
        or receipt.get("viewport") != list(sql_explorer_visual.VIEWPORT)
        or not isinstance(receipt.get("completed_at_epoch"), int)
        or not isinstance(scenes, dict)
        or not scenes
    ):
        raise RuntimeError(incomplete + "completion metadata is missing")
    for scene_id, scene in scenes.items():
        if not (
            isinstance(scene, dict)
            and scene.get("verdict") == "pass"
            and _sha256_hex(scene.get("screenshot_sha256"))
            and _sha256_hex(scene.get("geometry_sha256"))
        ):
            raise RuntimeError(incomplete + "scene evidence is missing for " + str(scene_id))
    try:
        session_file = sql_explorer_visual.session_path(root, review_id)
    except sql_explorer_visual.VisualReviewError as exc:
        raise RuntimeError(incomplete + str(exc)) from exc
    if session_file.is_symlink() or not session_file.is_file():
        raise RuntimeError(incomplete + "the review session record is missing")
    session = json.loads(session_file.read_text(encoding="utf-8"))
    capture = session.get("capture") if isinstance(session, dict) else None
    recorded = session.get("scenes") if isinstance(session, dict) else None
    if (
        not isinstance(capture, dict)
        or capture.get("status") != "pass"
        or capture.get("workspace_removed") is not True
        or not isinstance(recorded, dict)
        or set(recorded) != set(scenes)
        or any(
            not isinstance(recorded[scene_id], dict)
            or recorded[scene_id].get("capture") != "pass"
            or any(
                recorded[scene_id].get(key) != scene[key]
                for key in ("screenshot_sha256", "geometry_sha256", "verdict")
            )
            for scene_id, scene in scenes.items()
        )
        or any(
            session.get(key) != receipt.get(key)
            for key in ("review_id", "content_fingerprint", "manifest_sha256", "capture_mode")
        )
    ):
        raise RuntimeError(incomplete + "the review session does not match a completed capture")
