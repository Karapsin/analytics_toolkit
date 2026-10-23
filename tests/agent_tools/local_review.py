from __future__ import annotations

import hashlib
import io
import json
import sys
import uuid
from typing import TYPE_CHECKING

import pytest
from agent_tools.local_review_files import Files, complete_coverage, serve

from agent_tools import local_review, mcp_server, sessions, task_context
from agent_tools import release_preparation as preparation
from tests.agent_tools._support.local_review import signed_receipt
from tests.agent_tools.handoff import no_network, submitted
from tests.agent_tools.sessions import git
from tests.agent_tools.sessions import source as source  # noqa: PLC0414

if TYPE_CHECKING:
    from pathlib import Path

MANAGED_VALIDATION = local_review.validation


@pytest.fixture
def candidate(source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root, pr = submitted(source, tmp_path)
    no_network(monkeypatch, pr)
    monkeypatch.setattr(local_review, "validation", lambda root: {"ok": True, "fixture": True})
    monkeypatch.setattr(task_context, "repository_identity", lambda root: "owner/repo")
    return root


@pytest.mark.parametrize("failure", ["environment", "checks", None])
def test_release_preparation_obtains_review_before_real_push_gate(
    candidate: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    head = git(candidate, "rev-parse", "HEAD")
    (local_review.directory(candidate) / (head + ".json")).unlink()
    with pytest.raises(RuntimeError, match="local review is required"):
        sessions.push(candidate)
    pr = sessions.pr_info(candidate)
    order = []

    def environment(root, *, ensure_project_env):
        assert root == candidate
        assert ensure_project_env
        assert not (root / ".venv").exists()  # Unprepared fresh session clone.
        order.append("environment")
        return [("project", {"ok": failure != "environment"})], {}

    def checks(**kwargs):
        assert kwargs == {"level": "precommit", "root": str(candidate)}
        assert order == ["environment"]
        order.append("checks")
        return {"ok": failure != "checks"}

    def review(root):
        assert order == ["environment", "checks"]
        receipt = signed_receipt(root)
        pr["body"] = (
            task_context.attachment(root) + "\n" + local_review.protocol(root).attachment(receipt)
        )
        order.append("review")
        return receipt

    monkeypatch.setattr(mcp_server, "_verify_precommit_success", lambda root: {"ok": False})
    monkeypatch.setattr(mcp_server, "_prepare_environment", environment)
    monkeypatch.setattr(mcp_server, "run_checks", checks)
    monkeypatch.setattr(local_review, "review", review)
    found = iter([None, pr])
    monkeypatch.setattr(preparation, "_published_preparation", lambda *args: next(found))
    monkeypatch.setattr(preparation, "_recover_preparation_handoff", lambda *args: None)
    if failure:
        with pytest.raises(RuntimeError, match=r"setup failed|validation failed"):
            preparation._handoff_preparation(candidate, candidate, {"candidate_sha": head})
        assert "review" not in order
        assert sessions.load(candidate)["phase"] == "feature"
        return
    result = preparation._handoff_preparation(candidate, candidate, {"candidate_sha": head})
    assert result["number"] == pr["number"]
    assert order == ["environment", "checks", "review"]
    assert sessions.load(candidate)["handoffs"][-1]["sha"] == head


def test_published_receipt_is_bound_to_head_tree_task_and_private_enrollment(
    candidate: Path,
) -> None:
    head = git(candidate, "rev-parse", "HEAD")
    receipt = local_review.verified(candidate, head)
    proto = local_review.protocol(candidate)
    assert proto.extract(proto.attachment(receipt)) == receipt
    subject = local_review.binding(candidate, head)
    for key in ("head", "base", "tree", "task_digest", "producer_id", "repository"):
        changed = dict(subject, **{key: "wrong"})
        with pytest.raises(ValueError, match="exact source"):
            proto.validate_binding(receipt, **changed)
    receipt["payload"]["summary"] = "Changed after signing"
    with pytest.raises(ValueError, match="signature"):
        proto.verify_signature(receipt, local_review.directory(candidate) / "allowed-signers")


def test_task_change_and_missing_or_symlinked_receipts_invalidate_publication(
    candidate: Path,
) -> None:
    head = git(candidate, "rev-parse", "HEAD")
    target = local_review.directory(candidate) / (head + ".json")
    original = target.read_bytes()
    target.unlink()
    with pytest.raises(RuntimeError, match="local review is required"):
        local_review.verified(candidate, head)
    outside = target.with_name("outside")
    outside.write_bytes(original)
    target.symlink_to(outside)
    with pytest.raises(RuntimeError, match="local review is required"):
        local_review.verified(candidate, head)
    target.unlink()
    target.write_bytes(original)
    task_context.record(candidate, "A material correction after review", append=True)
    with pytest.raises(ValueError, match="signed head"):
        local_review.verified(candidate, head)


@pytest.mark.parametrize(
    "body",
    [
        "atk-local-review:v1",
        "<!-- atk-local-review:v1\nnot-base64!\n-->",
        "<!-- atk-local-review:v1\ne30=\n-->\n" * 2,
    ],
)
def test_malformed_or_duplicate_public_receipt_fails_closed(body: str, candidate: Path) -> None:
    with pytest.raises(ValueError, match=r"Malformed|Ambiguous|base64"):
        local_review.protocol(candidate).extract(body)


def test_immutable_git_pages_refuse_ignored_files_and_require_full_diff(candidate: Path) -> None:
    head = git(candidate, "rev-parse", "HEAD")
    ledger = local_review.directory(candidate) / "pages.jsonl"
    files = Files(candidate, head, sessions.load(candidate)["base"], ledger)
    (candidate / "feature").write_text("Unreviewed live mutation")
    assert "Unreviewed" not in files.read_file("feature")["content"]
    for path in ("../secret", ".git/config", ".connections", "untracked.env"):
        with pytest.raises(ValueError, match="Git blob"):
            files.read_file(path)
    with pytest.raises(ValueError, match="enumerate"):
        complete_coverage(files)
    assert "feature" in files.changed_files()
    with pytest.raises(ValueError, match="complete diff"):
        complete_coverage(files)
    for path in files.changed_files():
        offset = 0
        while True:
            value = files.read_diff(path, offset)
            if value["next_offset"] is None:
                break
            offset = value["next_offset"]
    complete_coverage(files)
    with pytest.raises(ValueError, match="offset"):
        files.read_diff("feature", -1)
    with pytest.raises(ValueError, match="inventory"):
        files.read_diff("not-changed")


def test_stdio_exposes_only_immutable_reads(
    candidate: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = Files(
        candidate,
        git(candidate, "rev-parse", "HEAD"),
        sessions.load(candidate)["base"],
        local_review.directory(candidate) / "stdio.jsonl",
    )
    requests = [
        {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/list"},
        {"id": 3, "method": "tools/call", "params": {"name": "write_file", "arguments": {}}},
        {
            "id": 4,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "feature"}},
        },
        {"id": 5, "method": "ping"},
        {"id": 6, "method": "unknown"},
    ]
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(json.dumps(x) for x in requests)))
    monkeypatch.setattr(sys, "stdout", output)
    serve(files)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(responses) == 6
    assert all(tool["annotations"]["readOnlyHint"] for tool in responses[1]["result"]["tools"])
    assert responses[2]["result"]["isError"]
    assert "feature" in responses[3]["result"]["content"][0]["text"]
    assert responses[-1]["error"]["code"] == -32601


PROGRAM = """
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[7])
from agent_tools.local_review_files import Files
root, job, head, base, mode, thread = sys.argv[1:7]
names = ('GH_TOKEN', 'GITHUB_TOKEN', 'OPENAI_API_KEY', 'CODEX_API_KEY', 'SSH_AUTH_SOCK')
assert not any(name in os.environ for name in names)
job = Path(job)
(job / 'reviewer-input.txt').write_text(sys.stdin.read(), encoding='utf-8')
files = Files(Path(root), head, base, job / 'served.jsonl')
if mode == 'failure':
    raise SystemExit(7)
for path in files.changed_files():
    if mode == 'incomplete':
        continue
    offset = 0
    while True:
        page = files.read_diff(path, offset)
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
if mode != 'missing-thread':
    print(json.dumps({'type': 'thread.started', 'thread_id': thread}))
verdict = {'approved': mode != 'reject', 'alignment': 'aligned',
           'summary': 'Synthetic reviewer result', 'findings': [], 'change_summary': 'Feature'}
(job / 'verdict.json').write_text(json.dumps(verdict))
"""


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("approve", None),
        ("reject", "rejected"),
        ("incomplete", "complete diff"),
        ("failure", "did not complete"),
        ("missing-thread", "fresh reviewer"),
    ],
)
def test_fresh_review_execution_and_fail_closed_publication(
    candidate: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    error: str | None,
) -> None:
    original = task_context.read(candidate)
    replacement = "/replace_reference " + json.dumps(
        {
            "reference": "Implement the plan",
            "replacement": (
                "Implement the workflow redesign. Automations remain stopped; PR #9 is "
                "outstanding and deferred outside this PR. Separate Pi agents repair CI "
                "failures and independently review changed heads/bases. No new retry limit."
            ),
        }
    )
    context = {
        **original,
        "version": 2,
        "starting_prompt": "stop all current gh automations. concentrate on merging PR #9",
        "corrections": [
            "Implement the plan.",
            "Keep the orchestrator; initial review stays on the developing machine.",
            replacement,
        ],
        "plans": [],
        "images": [],
        "replacements": [{"reference": "Implement the plan", "user_index": 2}],
        "decisions": [],
    }
    active = task_context.directory(candidate) / "active.json"
    active.write_text(json.dumps(context), encoding="utf-8")
    git(
        candidate,
        *sessions.commit_options(candidate),
        "commit",
        "--allow-empty",
        "-m",
        "Amended workflow task" + task_context.commit_trailers(candidate),
    )
    head = git(candidate, "rev-parse", "HEAD")
    thread = str(uuid.uuid4())
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-never-forwarded")
    monkeypatch.setattr(
        local_review,
        "arguments",
        lambda root, job, subject: [
            sys.executable,
            "-c",
            PROGRAM,
            str(root),
            str(job),
            subject["head"],
            subject["base"],
            mode,
            thread,
            str(local_review.Path(__file__).resolve().parents[2]),
        ],
    )
    if error:
        with pytest.raises((RuntimeError, ValueError), match=error):
            local_review.review(candidate)
        assert not (local_review.directory(candidate) / (head + ".json")).exists()
    else:
        receipt = local_review.review(candidate)
        assert receipt["payload"]["head"] == head
        assert receipt["payload"]["reviewer_id"] == "reviewer:" + thread
        assert local_review.review(candidate) == receipt
    inputs = list(local_review.directory(candidate).glob("run-*/reviewer-input.txt"))
    assert len(inputs) == 1
    prompt = inputs[0].read_text(encoding="utf-8")
    payload = json.loads(prompt[prompt.index("{"):])
    assert payload["user_task"] == task_context.protocol().public_context(context)
    assert payload["user_task"]["starting_prompt"] == context["starting_prompt"]
    assert payload["user_task"]["corrections"] == context["corrections"]
    assert payload["user_task"]["replacements"] == context["replacements"]
    assert "after prior tasks were cancelled" not in prompt
    assert "Do not infer cancellation of earlier obligations" in prompt
    assert "explicitly deferred outside this PR" in prompt


def test_reviewer_cli_cannot_resume_author_or_enable_shell(
    candidate: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_review.shutil, "which", lambda name: "/synthetic/codex")
    monkeypatch.setattr(
        local_review,
        "trusted_runtime",
        lambda root, job, base: (job / "source_provider.py", job / "schema.json"),
    )
    job = local_review.directory(candidate) / "job"
    args = local_review.arguments(
        candidate, job, local_review.binding(candidate, git(candidate, "rev-parse", "HEAD"))
    )
    assert "resume" not in args
    assert "--ephemeral" in args
    assert "--ignore-user-config" in args
    assert args[args.index("-C") + 1] == str(job)
    for feature in ("shell_tool", "unified_exec", "plugins", "apps"):
        assert args[args.index(feature) - 1] == "--disable"


def test_trusted_runtime_uses_base_objects_and_rejects_unreviewed_bootstrap(candidate, tmp_path):
    base = git(candidate, "rev-parse", "HEAD")
    job = tmp_path / "isolated"
    job.mkdir()
    bootstrap = sessions.state_path(candidate).with_name("atk-local-review-bootstrap")
    (bootstrap / "manifest.json").unlink()
    with pytest.raises(FileNotFoundError):
        local_review.trusted_runtime(candidate, job, base)
    bootstrap = sessions.state_path(candidate).with_name("atk-local-review-bootstrap")
    bootstrap.mkdir(exist_ok=True)
    files = {
        "agent_tools/local_review_files.py": b"# reviewed provider\n",
        ".github/agent/review.schema.json": b"{}\n",
    }
    manifest = {"reviewer_id": "synthetic-independent-reviewer", "files": {}}
    for name, data in files.items():
        (bootstrap / local_review.Path(name).name).write_bytes(data)
        manifest["files"][name] = hashlib.sha256(data).hexdigest()
    (bootstrap / "manifest.json").write_text(json.dumps(manifest))
    source, schema = local_review.trusted_runtime(candidate, job, base)
    assert source.read_bytes() == files["agent_tools/local_review_files.py"]
    assert schema.read_bytes() == b"{}\n"
    (bootstrap / "local_review_files.py").write_text("# tampered")
    with pytest.raises(ValueError, match="digest mismatch"):
        local_review.trusted_runtime(candidate, job, base)
    manifest["reviewer_id"] = ""
    (bootstrap / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="reviewer identity"):
        local_review.trusted_runtime(candidate, job, base)
    for name, data in files.items():
        target = candidate / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    git(candidate, "add", "agent_tools", ".github")
    git(candidate, "-c", "commit.gpgsign=false", "commit", "-m", "Reviewed runtime")
    base = git(candidate, "rev-parse", "HEAD")
    (candidate / "agent_tools/local_review_files.py").write_text("# candidate mutation")
    assert (
        local_review.trusted_runtime(candidate, job, base)[0].read_bytes()
        == files["agent_tools/local_review_files.py"]
    )


def test_binary_review_requires_original_png_and_denies_connection_images(candidate, tmp_path):
    base = git(candidate, "rev-parse", "HEAD")
    (candidate / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00synthetic")
    git(candidate, "add", "image.png")
    git(candidate, "-c", "commit.gpgsign=false", "commit", "-m", "Image")
    files = Files(candidate, git(candidate, "rev-parse", "HEAD"), base, tmp_path / "ledger")
    files.changed_files()
    files.read_diff("image.png")
    with pytest.raises(ValueError, match="reviewed PNG"):
        complete_coverage(files)
    assert files.read_image("image.png")["mime_type"] == "image/png"
    complete_coverage(files)
    with pytest.raises(ValueError, match="tracked PNG"):
        files.read_image(".connections/secret.png")
    with pytest.raises(ValueError, match="base Git blob"):
        files.read_base_file(".connections")


def test_large_unicode_diff_requires_every_page_and_handles_deletion(candidate, tmp_path):
    base = git(candidate, "rev-parse", "HEAD")
    (candidate / "feature").unlink()
    (candidate / "unicode").write_text("Ж" * 60000)
    git(candidate, "add", "--all")
    git(candidate, "-c", "commit.gpgsign=false", "commit", "-m", "Paged change")
    files = Files(candidate, git(candidate, "rev-parse", "HEAD"), base, tmp_path / "ledger")
    assert "feature" in files.changed_files()
    files.read_diff("feature")
    page = files.read_diff("unicode")
    assert page["next_offset"] is not None
    with pytest.raises(ValueError, match="complete diff"):
        complete_coverage(files)
    while page["next_offset"] is not None:
        page = files.read_diff("unicode", page["next_offset"])
    complete_coverage(files)
    assert files.read_base_file("feature")["content"] == "feature\n"


def test_certification_never_executes_candidate_protocol_or_provider(candidate):
    target = candidate / ".github/agent/local_review.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('raise RuntimeError("candidate protocol executed")\n')
    provider = candidate / "agent_tools/local_review_files.py"
    provider.parent.mkdir(parents=True, exist_ok=True)
    provider.write_text('raise RuntimeError("candidate provider executed")\n')
    assert local_review.protocol(candidate).MARKER == "atk-local-review:v1"
    job = local_review.directory(candidate) / "trusted-validator"
    job.mkdir()
    source, _ = local_review.trusted_runtime(candidate, job, sessions.load(candidate)["base"])
    assert b"candidate provider executed" not in source.read_bytes()


def test_private_evidence_binds_task_and_rejects_symlink_or_oversize(candidate, tmp_path):
    path = sessions.state_path(candidate).with_name("atk-local-review-evidence.json")
    assert local_review.evidence(candidate, "digest") == {}
    path.write_text(json.dumps({"task_digest": "digest", "scope": "Approved scope"}))
    assert local_review.evidence(candidate, "digest")["scope"] == "Approved scope"
    with pytest.raises(ValueError, match="different task"):
        local_review.evidence(candidate, "wrong")
    outside = tmp_path / "outside"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="unsafe or oversized"):
        local_review.evidence(candidate, "digest")
    path.unlink()
    path.write_bytes(b"x" * (128 * 1024 + 1))
    with pytest.raises(ValueError, match="unsafe or oversized"):
        local_review.evidence(candidate, "digest")


@pytest.mark.parametrize(
    "message",
    [
        "No successful pre-commit check record exists.",
        "Recorded pre-commit check fingerprint does not match the current tree.",
    ],
)
def test_review_refuses_missing_or_stale_validation_before_using_cache(
    candidate,
    monkeypatch,
    message,
):
    monkeypatch.setattr(local_review, "validation", MANAGED_VALIDATION)
    monkeypatch.setattr(
        mcp_server, "_verify_precommit_success", lambda root: {"ok": False, "message": message}
    )
    with pytest.raises(RuntimeError, match="Exact-tree mandatory validation"):
        local_review.review(candidate)


def test_validation_evidence_includes_the_managed_receipt(candidate, monkeypatch):
    monkeypatch.setattr(
        mcp_server,
        "_verify_precommit_success",
        lambda root: {"ok": True, "fingerprint": "exact-tree"},
    )
    path = candidate / mcp_server.CHECK_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {"fingerprint": "exact-tree", "stages": ["static", "coverage", "artifacts"]}
    path.write_text(json.dumps(receipt))
    assert MANAGED_VALIDATION(candidate) == {
        "verification": {"ok": True, "fingerprint": "exact-tree"},
        "receipt": receipt,
    }
