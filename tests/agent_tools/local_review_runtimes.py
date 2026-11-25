"""Fresh local review through the shared runtime adapter for both CLIs; no model calls."""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from typing import TYPE_CHECKING

import pytest

from agent_tools import agent_runtimes, local_review, sessions
from tests._support.paths import REPO_ROOT
from tests.agent_tools.local_review import candidate as candidate  # noqa: PLC0414
from tests.agent_tools.local_review_evidence import commit
from tests.agent_tools.sessions import git
from tests.agent_tools.sessions import source as source  # noqa: PLC0414

if TYPE_CHECKING:
    from pathlib import Path

FAKE = """
import json, os, sys
from pathlib import Path
sys.path.insert(0, {repo!r})
from agent_tools.local_review_files import Files
control = Path({control!r})
mode = (control / "mode").read_text()
args = sys.argv[1:]
if args == ["--version"]:
    print({version!r})
    raise SystemExit(0)
(control / "argv.json").write_text(json.dumps(args))
(control / "env.json").write_text(json.dumps(sorted(os.environ)))
(control / "cwd").write_text(os.getcwd())
(control / "input.txt").write_text(sys.stdin.read(), encoding="utf-8")
if {runtime!r} == "claude":
    session = args[args.index("--session-id") + 1]
    server = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())
    provider = server["mcpServers"]["source_files"]["args"]
else:
    session = str(__import__("uuid").uuid4())
    prefix = "mcp_servers.source_files.args="
    provider = next(json.loads(v[len(prefix):]) for v in args if v.startswith(prefix))
root, head, base, ledger = provider[3], provider[5], provider[7], provider[9]
if mode == "failure":
    raise SystemExit(7)
files = Files(Path(root), head, base, Path(ledger))
for path in files.changed_files():
    if mode == "incomplete":
        continue
    offset = 0
    while True:
        page = files.read_diff(path, offset)
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
verdict = {{"approved": mode != "reject", "alignment": "aligned", "summary": "Synthetic review",
           "findings": [], "change_summary": "Feature"}}
if mode == "malformed":
    verdict.pop("findings")
if {runtime!r} == "codex":
    print(json.dumps({{"type": "thread.started", "thread_id": session}}))
    Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(verdict))
    raise SystemExit(0)
tools = ["mcp__source_files__" + name for name in
         ("changed_files", "list_files", "read_file", "read_base_file", "read_diff", "read_image")]
tools.append("StructuredOutput")
init = {{"type": "system", "subtype": "init", "session_id": session, "tools": tools,
        "mcp_servers": [{{"name": "source_files", "status": "connected"}}],
        "permissionMode": "dontAsk", "apiKeySource": "none", "model": "claude-synthetic",
        "claude_code_version": "2.1.310"}}
result = {{"type": "result", "subtype": "success", "is_error": False, "session_id": session,
          "structured_output": verdict}}
if mode == "wrong-session":
    init["session_id"] = {thread!r}
if mode == "extra-tool":
    init["tools"].append("Bash")
if mode == "extra-server":
    init["mcp_servers"].append({{"name": "analytics_toolkit_agent_tools", "status": "connected"}})
if mode == "api-key":
    init["apiKeySource"] = "ANTHROPIC_API_KEY"
if mode == "permission-mode":
    init["permissionMode"] = "default"
if mode == "no-structured":
    result.pop("structured_output")
if mode == "error-result":
    result["subtype"] = "error_max_turns"
events = [init, result]
if mode == "double-init":
    events.insert(0, dict(init))
for event in events:
    print(json.dumps(event))
"""


@pytest.fixture
def adapter_base(candidate: Path) -> Path:
    """Serve the reviewed adapter through the digest-pinned trusted bootstrap manifest."""
    bootstrap = sessions.state_path(candidate).with_name("atk-local-review-bootstrap")
    manifest = json.loads((bootstrap / "manifest.json").read_text())
    data = (REPO_ROOT / local_review.RUNTIME_ADAPTER).read_bytes()
    (bootstrap / "agent_runtimes.py").write_bytes(data)
    manifest["files"][local_review.RUNTIME_ADAPTER] = hashlib.sha256(data).hexdigest()
    (bootstrap / "manifest.json").write_text(json.dumps(manifest))
    head = git(candidate, "rev-parse", "HEAD")
    (local_review.directory(candidate) / (head + ".json")).unlink()
    fresh_head = commit(candidate, {"runtime_probe.py": "PROBE = 'actual reviewer runtime'\n"})
    assert fresh_head != head
    return candidate


def install_fake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str, mode: str
) -> tuple[Path, str]:
    control = tmp_path / ("control-" + runtime)
    control.mkdir()
    (control / "mode").write_text(mode)
    thread = str(uuid.uuid4())
    script = control / "fake.py"
    script.write_text(
        FAKE.format(
            repo=str(REPO_ROOT),
            control=str(control),
            runtime=runtime,
            thread=thread,
            version="2.1.310 (Claude Code)" if runtime == "claude" else "codex-cli 9.9.9",
        )
    )
    binary = tmp_path / ("bin-" + runtime)
    binary.mkdir()
    name = "claude" if runtime == "claude" else "codex"
    (binary / name).write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    (binary / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + ":/usr/bin:/bin")
    return control, thread


def _assert_private_runtime_proof(root, payload, runtime):
    compact = payload["review_iterations"]
    assert compact["version"] == 4
    proof_path = local_review.directory(root) / ("proof-" + compact["proof_sha256"] + ".json")
    data = proof_path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == compact["proof_sha256"]
    proof = json.loads(data)
    assert proof["version"] == 4
    waves = proof["waves"]
    assert len(waves) == compact["completed_waves"] == 1
    assert len({wave["payload"]["wave_id"] for wave in waves}) == 1
    reviewers = []
    for wave in waves:
        outcome = wave["payload"]
        assert outcome["review_scope"] == "wave"
        assert outcome["coverage_complete"] is True
        assert outcome["discovery_only"] is False
        assert outcome["verdict"]["approved"] is True
        children = outcome["review_wave"]["children"]
        origins = outcome["review_wave"]["origins"]
        assert len(children) == len(origins) == 1
        assert {child["payload"]["chunk_id"] for child in children} == {
            chunk["id"] for chunk in outcome["review_wave"]["manifest"]["chunks"]
        }
        for child, origin in zip(children, origins):
            observed = child["payload"]
            assert observed["head"] == payload["head"]
            assert origin["subject"]["head"] == payload["head"]
            assert origin["manifest"] == outcome["review_wave"]["manifest"]
            assert origin["wave_id"] == observed["wave_id"]
            assert observed["review_scope"] == "chunk"
            assert observed["wave_id"] == outcome["wave_id"]
            assert observed["coverage_complete"] is True
            assert observed["reviewer_runtime"] == (
                "claude-code" if runtime == "claude" else "codex"
            )
            reviewer = observed["reviewer_id"]
            assert reviewer.startswith("reviewer:")
            identity = reviewer.removeprefix("reviewer:")
            assert str(uuid.UUID(identity, version=4)) == identity
            reviewers.append(reviewer)
    assert len(reviewers) == len(set(reviewers)) == compact["reviewer_count"]
    assert payload["reviewer_id"] == waves[-1]["payload"]["reviewer_id"]


@pytest.mark.parametrize("weakened", [False, True])
def test_legacy_codex_adapter_gets_fixed_high_effort_without_losing_isolation(
    adapter_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, weakened: bool
) -> None:
    """An older immutable adapter lacks effort; the caller must retain all its flags."""
    install_fake(tmp_path, monkeypatch, "codex", "approve")
    monkeypatch.setenv("ATK_LOCAL_REVIEWER", "codex")
    bootstrap = sessions.state_path(adapter_base).with_name("atk-local-review-bootstrap")
    path = bootstrap / "agent_runtimes.py"
    legacy = (
        path.read_text()
        + """
from dataclasses import replace as _legacy_replace
_original_codex_reviewer = RUNTIMES["codex"].reviewer_argv
def _legacy_codex_reviewer(*args):
    global legacy_command
    command = _original_codex_reviewer(*args)
    legacy_command = []
    index = 0
    while index < len(command):
        if (command[index] == "-c" and index + 1 < len(command)
                and command[index + 1].startswith("model_reasoning_effort=")):
            index += 2
        else:
            legacy_command.append(command[index])
            index += 1
    if WEAKENED:
        legacy_command.remove("--ephemeral")
    return list(legacy_command)
RUNTIMES["codex"] = _legacy_replace(RUNTIMES["codex"], reviewer_argv=_legacy_codex_reviewer)
""".replace("WEAKENED", repr(weakened))
    )
    path.write_text(legacy)
    manifest_path = bootstrap / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][local_review.RUNTIME_ADAPTER] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    subject = local_review.binding(adapter_base, git(adapter_base, "rev-parse", "HEAD"))
    job = tmp_path / "review-job"
    job.mkdir()
    if weakened:
        with pytest.raises(RuntimeError, match="Reviewer isolation policy is incomplete"):
            local_review._reviewer_command(adapter_base, job, subject, str(uuid.uuid4()))
        return
    runtime, adapter, command, _environment = local_review._reviewer_command(
        adapter_base, job, subject, str(uuid.uuid4())
    )
    assert runtime == "codex"
    assert command[-3:] == ["-c", 'model_reasoning_effort="high"', "-"]
    assert [*command[:-3], "-"] == adapter.legacy_command
    assert agent_runtimes.policy_violations(runtime, command) == []
    assert "-m" not in command
    assert "--model" not in command


@pytest.mark.parametrize("runtime", ["codex", "claude"])
def test_adapter_review_records_runtime_identity_and_isolation(
    adapter_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    control, _thread = install_fake(tmp_path, monkeypatch, runtime, "approve")
    monkeypatch.setenv("ATK_LOCAL_REVIEWER", runtime)
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-never-forwarded")
    monkeypatch.setenv("ATK_UNLISTED_PROBE", "synthetic-unlisted")
    receipt = local_review.review(adapter_base)
    payload = receipt["payload"]
    assert payload["head"] == git(adapter_base, "rev-parse", "HEAD")
    args = json.loads((control / "argv.json").read_text())
    environment = json.loads((control / "env.json").read_text())
    assert agent_runtimes.policy_violations(runtime, [runtime, *args]) == []
    assert "GITHUB_TOKEN" not in environment
    assert ("ATK_UNLISTED_PROBE" in environment) is (runtime == "codex")
    cwd = (control / "cwd").read_text()
    assert not cwd.startswith(str(adapter_base.resolve()))
    prompt = (control / "input.txt").read_text(encoding="utf-8")
    assert prompt.startswith(agent_runtimes.REVIEW_PROMPT)
    if runtime == "claude":
        assert not any(value.startswith("model_reasoning_effort=") for value in args)
        session = args[args.index("--session-id") + 1]
        assert str(uuid.UUID(session, version=4)) == session
        assert payload["reviewer_id"] == "reviewer:" + session
        assert payload["reviewer_runtime"] == "claude-code"
        assert payload["reviewer_version"] == "2.1.310"
        assert payload["reviewer_model"] == "claude-synthetic"
        assert payload["reviewer_auth"] == "claude-subscription"
        assert args[args.index("--tools") + 1] == ""
        assert json.loads(args[args.index("--json-schema") + 1])["additionalProperties"] is False
    else:
        assert args[-3:] == ["-c", 'model_reasoning_effort="high"', "-"]
        assert payload["reviewer_id"].startswith("reviewer:")
        assert payload["reviewer_runtime"] == "codex"
        assert payload["reviewer_version"] == "codex-cli 9.9.9"
        assert payload["reviewer_auth"] == "chatgpt"
    _assert_private_runtime_proof(adapter_base, payload, runtime)
    assert local_review.review(adapter_base) == receipt


@pytest.mark.parametrize(
    ("runtime", "trusted_timeout"),
    [("codex", 1800), ("codex", 7200), ("claude", 1800), ("claude", 7200), ("codex", None)],
)
def test_source_review_budget_applies_to_trusted_adapters_and_codex_fallback(
    adapter_base: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
    trusted_timeout: int | None,
) -> None:
    install_fake(tmp_path, monkeypatch, runtime, "approve")
    monkeypatch.setenv("ATK_LOCAL_REVIEWER", runtime)
    bootstrap = sessions.state_path(adapter_base).with_name("atk-local-review-bootstrap")
    adapter_path = bootstrap / "agent_runtimes.py"
    manifest_path = bootstrap / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if trusted_timeout is None:
        manifest["files"].pop(local_review.RUNTIME_ADAPTER)
        adapter_path.unlink()
    else:
        adapter_path.write_text(
            adapter_path.read_text() + f"\nREVIEW_TIMEOUT = {trusted_timeout!r}\n"
        )
        manifest["files"][local_review.RUNTIME_ADAPTER] = hashlib.sha256(
            adapter_path.read_bytes()
        ).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    executed = []
    real_run = local_review.subprocess.run

    def capture_review_execution(command, *args, **kwargs):
        if (
            isinstance(command, (list, tuple))
            and command
            and str(command[0]).rsplit("/", 1)[-1] == runtime
            and "input" in kwargs
        ):
            executed.append((list(command), kwargs.get("timeout")))
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(local_review.subprocess, "run", capture_review_execution)
    receipt = local_review.review(adapter_base)
    assert agent_runtimes.REVIEW_TIMEOUT == 7200
    assert local_review.REVIEW_TIMEOUT == agent_runtimes.REVIEW_TIMEOUT
    assert [timeout for _command, timeout in executed] == [7200]
    assert all(
        agent_runtimes.policy_violations(runtime, command) == [] for command, _timeout in executed
    )
    _assert_private_runtime_proof(adapter_base, receipt["payload"], runtime)
    assert local_review.verified(adapter_base, receipt["payload"]["head"]) == receipt
    assert local_review.review(adapter_base) == receipt
    assert [timeout for _command, timeout in executed] == [7200]


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("reject", "rejected"),
        ("malformed", "rejected"),
        ("incomplete", "complete diff"),
        ("failure", "did not complete"),
        ("wrong-session", "isolation or result"),
        ("extra-tool", "isolation or result"),
        ("extra-server", "isolation or result"),
        ("api-key", "isolation or result"),
        ("permission-mode", "isolation or result"),
        ("no-structured", "isolation or result"),
        ("error-result", "isolation or result"),
        ("double-init", "fresh reviewer"),
    ],
)
def test_claude_reviewer_fails_closed(
    adapter_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, error: str
) -> None:
    install_fake(tmp_path, monkeypatch, "claude", mode)
    monkeypatch.setenv("ATK_LOCAL_REVIEWER", "claude")
    head = git(adapter_base, "rev-parse", "HEAD")
    with pytest.raises((RuntimeError, ValueError), match=error):
        local_review.review(adapter_base)
    assert not (local_review.directory(adapter_base) / (head + ".json")).exists()


def test_reviewer_session_cannot_reuse_an_author_session(
    adapter_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake(tmp_path, monkeypatch, "claude", "approve")
    fixed = uuid.uuid4()
    monkeypatch.setattr(local_review.uuid, "uuid4", lambda: fixed)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", str(fixed))
    with pytest.raises(RuntimeError, match="separate fresh reviewer"):
        local_review.review(adapter_base)


def test_missing_selected_cli_fails_closed_without_fallback(
    adapter_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake(tmp_path, monkeypatch, "codex", "approve")
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(RuntimeError, match="Claude Code CLI is required"):
        local_review.review(adapter_base)


def test_claude_reviewer_requires_the_adapter_in_the_base(
    candidate: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = git(candidate, "rev-parse", "HEAD")
    (local_review.directory(candidate) / (head + ".json")).unlink()
    fresh_head = commit(candidate, {"missing_adapter_probe.py": "PROBE = 'fresh Claude scope'\n"})
    assert fresh_head != head
    monkeypatch.setenv("ATK_LOCAL_REVIEWER", "claude")
    monkeypatch.setattr(local_review, "arguments", lambda *args: pytest.fail("codex fallback"))
    with pytest.raises(RuntimeError, match="requires the reviewed runtime adapter"):
        local_review.review(candidate)
    assert not (local_review.directory(candidate) / (fresh_head + ".json")).exists()


def test_tampered_adapter_is_never_executed(adapter_base: Path, tmp_path: Path) -> None:
    bootstrap = sessions.state_path(adapter_base).with_name("atk-local-review-bootstrap")
    (bootstrap / "agent_runtimes.py").write_text('raise RuntimeError("tampered adapter")\n')
    job = tmp_path / "job"
    job.mkdir()
    with pytest.raises(ValueError, match="digest mismatch"):
        local_review.runtime_adapter(adapter_base, job, sessions.load(adapter_base)["base"])


@pytest.mark.parametrize(
    ("environment", "state", "expected"),
    [
        ({"ATK_LOCAL_REVIEWER": "codex", "CLAUDECODE": "1"}, {"reviewer": "claude"}, "codex"),
        ({"CLAUDECODE": "1"}, {"reviewer": "codex", "runtime": "claude"}, "codex"),
        ({"CODEX_THREAD_ID": "t"}, {"runtime": "claude"}, "claude"),
        ({"CLAUDECODE": "1"}, {}, "claude"),
        ({"CODEX_THREAD_ID": "t"}, {}, "codex"),
        ({}, {}, "codex"),
    ],
)
def test_reviewer_selection_precedence_matches_built_in(
    candidate: Path, monkeypatch: pytest.MonkeyPatch, environment, state, expected
) -> None:
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    stored = sessions.load(candidate)
    stored.pop("reviewer", None)
    stored.pop("runtime", None)
    sessions.save(candidate, {**stored, **state})
    assert agent_runtimes.select_reviewer(environment, state) == expected
    assert local_review.select_reviewer(candidate) == expected
    monkeypatch.setenv("ATK_LOCAL_REVIEWER", "gemini")
    with pytest.raises(RuntimeError, match="Unknown local reviewer"):
        local_review.select_reviewer(candidate)
    with pytest.raises(RuntimeError, match="Unknown local reviewer"):
        agent_runtimes.select_reviewer({"ATK_LOCAL_REVIEWER": "gemini"}, {})
