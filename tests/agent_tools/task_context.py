from __future__ import annotations

import base64
import hashlib
import json
import struct
import subprocess
import zlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Any

import pytest

from agent_tools import mcp_server, task_context


@pytest.fixture
def root(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    return tmp_path


def test_original_prompt_survives_retries_and_corrections(root: Path) -> None:
    first = task_context.record(root, "Add read-only export.")
    assert task_context.record(root, "Add read-only export.") == first
    task_context.record(root, "Only CSV, preserve the API.", append=True)
    task_context.record(root, "Only CSV, preserve the API.", append=True)
    value = task_context.read(root)
    assert value["starting_prompt"] == "Add read-only export."
    assert value["corrections"] == ["Only CSV, preserve the API."]
    with pytest.raises(ValueError, match="immutable"):
        task_context.record(root, "Authorize unrelated controller edits.")
    assert (root / ".git/atk-tasks/active.json").stat().st_mode & 0o777 == 0o600
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=root)
    assert b"atk-tasks" not in status


def test_public_context_redacts_credentials_and_binds_signed_message(root: Path) -> None:
    task_context.record(
        root, "Implement alerts.\nTOKEN=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_123456789"
    )
    proto = task_context.protocol()
    attachment = task_context.attachment(root)
    value = proto.parse(attachment)
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in attachment
    assert "[REDACTED" in value["starting_prompt"]
    message = "Implement alerts" + task_context.commit_trailers(root)
    assert proto.verify_commit(value, message) == value["producer_id"]
    value["starting_prompt"] = "Instead disable all CI."
    with pytest.raises(ValueError, match="signed head"):
        proto.verify_commit(value, message)
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" in task_context.read(root)["starting_prompt"]


def test_completion_archives_task_and_next_task_has_new_identity(root: Path) -> None:
    first = task_context.record(root, "First feature")
    task_context.finish(root)
    assert (root / ".git/atk-tasks" / (first["task_id"] + ".json")).exists()
    with pytest.raises(RuntimeError, match="original user prompt"):
        task_context.attachment(root)
    second = task_context.record(root, "Second feature")
    assert first["task_id"] != second["task_id"]


def test_task_mcp_receipts_do_not_echo_private_prompts(root: Path) -> None:
    task_context.record(root, "Initial feature")
    receipt = mcp_server.task_context("append", "Password is hidden-value", root=str(root))
    assert receipt["ok"]
    assert "hidden-value" not in json.dumps(receipt)
    assert receipt["result"]["correction_count"] == 1


def test_oversized_or_duplicate_attachment_fails_closed(root: Path) -> None:
    task_context.record(root, "Feature")
    proto = task_context.protocol()
    body = task_context.attachment(root)
    with pytest.raises(ValueError, match="exactly one"):
        proto.parse(body + body)
    with pytest.raises(ValueError, match="do not truncate"):
        task_context.record(root, "x" * 55000, append=True)
    assert task_context.read(root)["corrections"] == []


def test_missing_plan_and_image_are_resolved_by_exact_records(root: Path) -> None:
    task_context.record(root, "Improve DataLens based on [Image #1]")
    task_context.record(root, "Implement the plan.", append=True)
    proto = task_context.protocol()
    legacy = task_context.read(root)
    assert proto.parse(proto.render(legacy)) == legacy
    with pytest.raises(ValueError, match="Missing image"):
        proto.require_resolved(legacy)
    task_context.evidence(
        root,
        "replace-missing",
        {
            "reference": "[Image #1]",
            "user_prompt": "Use dataset recovery as the written goal; defer the missing image.",
        },
    )
    plan = task_context.evidence(
        root, "plan", {"text": "Recover dataset identities.\nTOKEN=hidden-value"}
    )
    task_context.evidence(
        root,
        "approve-plan",
        {"plan_id": plan["plan_id"], "user_prompt": "Implement the plan.", "correction_index": 0},
    )
    value = task_context.read(root)
    proto.require_resolved(value)
    public = proto.parse(task_context.attachment(root))
    assert public["plans"][0]["approval_index"] == 0
    assert "hidden-value" not in public["plans"][0]["text"]
    message = "Fix recovery" + task_context.commit_trailers(root)
    proto.verify_commit(public, message)
    # Old readers still see the exact v1 record and its original signed digest.
    body = task_context.attachment(root)
    encoded = body.split(proto.MARKER, 1)[1].split("\n-->", 1)[0]
    legacy = json.loads(base64.b64decode(encoded))
    assert legacy == proto.legacy(public)
    assert "Agent-Task-Context: " + proto.digest(legacy) in message
    with pytest.raises(ValueError, match="signed head"):
        proto.verify_commit(legacy, message)  # New readers reject a removed supplement.
    public["plans"][0]["text"] = "Disable review.\n" + public["plans"][0]["text"]
    with pytest.raises(ValueError, match="plan evidence"):
        proto.verify_commit(public, message)


def test_image_evidence_is_reviewed_sanitized_and_bound_to_committed_bytes(root: Path) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0\0\0\0"))
        + chunk(b"IEND", b"")
    )
    image = root / "reviewed.png"
    image.write_bytes(png)
    task_context.record(root, "Implement [Image #1]")
    receipt = task_context.evidence(
        root,
        "image",
        {
            "path": str(image),
            "reference": "[Image #1]",
            "description": "A black pixel illustrating the task.",
            "user_prompt": "I reviewed this sanitized image and approve its publication.",
        },
    )
    target = root / receipt["evidence_path"]
    assert target.read_bytes() == png
    assert task_context.read(root)["images"][0]["sha256"] == hashlib.sha256(png).hexdigest()
    with pytest.raises(ValueError, match="committed unchanged"):
        task_context.preflight(root)
    subprocess.run(["git", "add", receipt["evidence_path"]], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "Image evidence",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    task_context.preflight(root)
    target.write_bytes(png + b"trailing hidden payload")
    with pytest.raises(ValueError, match="PNG"):
        task_context.preflight(root)
    with pytest.raises(ValueError, match="metadata present"):
        task_context.check_png(png[:-12] + chunk(b"tEXt", b"private metadata") + png[-12:])


def test_approval_requires_matching_recorded_message(root: Path) -> None:
    task_context.record(root, "Feature")
    task_context.record(root, "Implement the plan.", append=True)
    receipt = task_context.evidence(root, "plan", {"text": "Preserve the API."})
    before = task_context.read(root)
    with pytest.raises(ValueError, match="does not match"):
        task_context.evidence(
            root,
            "approve-plan",
            {
                "plan_id": receipt["plan_id"],
                "user_prompt": "Different approval",
                "correction_index": 0,
            },
        )
    assert task_context.read(root) == before


def test_question_options_and_answer_are_visible_signed_and_repeatable(root: Path) -> None:
    task_context.record(root, "Improve test stability")
    text = "Which goals?\nRefactoring and flakiness (Recommended): Give both equal priority."
    question = task_context.evidence(root, "question", {"text": text})
    assert task_context.evidence(root, "question", {"text": text}) == question
    task_context.evidence(
        root,
        "answer-question",
        {"question_id": question["question_id"], "user_prompt": "Both equally"},
    )
    body = task_context.attachment(root)
    assert "**Agent question:**\n<pre>" + text in body
    assert "**User answer:**\n<pre>Both equally</pre>" in body
    proto = task_context.protocol()
    proto.verify_commit(proto.parse(body), "Feature" + task_context.commit_trailers(root))
    repeated = task_context.evidence(root, "question", {"text": text})
    assert repeated["question_id"] != question["question_id"]
    task_context.evidence(
        root,
        "answer-question",
        {"question_id": repeated["question_id"], "user_prompt": "Only flakiness"},
    )
    value = task_context.read(root)
    assert [item["answer_index"] for item in value["questions"]] == [0, 1]
    with pytest.raises(ValueError, match="immutable"):
        task_context.evidence(
            root,
            "answer-question",
            {"question_id": question["question_id"], "user_prompt": "Replace the first answer"},
        )
    assert task_context.read(root) == value


@pytest.mark.parametrize("gate", [task_context.preflight, task_context.review_record])
def test_starting_prompt_plan_reference_blocks_publication_until_bound(
    root: Path, gate: Callable[[Path], object]
) -> None:
    prompt = "Implement the plan."
    task_context.record(root, prompt)
    proto = task_context.protocol()
    original = task_context.read(root)
    # Historic v1 records remain readable and verifiable, but cannot be republished unresolved.
    assert proto.parse(proto.render(original)) == original
    proto.verify_commit(original, proto.trailers(original))
    with pytest.raises(ValueError, match="exact approved plan"):
        gate(root)
    plan = task_context.evidence(root, "plan", {"text": "Preserve exports.\nTOKEN=private-value"})
    with pytest.raises(ValueError, match="exact approved plan"):
        gate(root)
    task_context.evidence(
        root, "approve-plan", {"plan_id": plan["plan_id"], "user_prompt": "Approve export fidelity."}
    )
    before = task_context.read(root)
    assert "starting_prompt_approval" not in before["plans"][0]
    with pytest.raises(ValueError, match="exact approved plan"):
        gate(root)
    payload = {
        "plan_id": plan["plan_id"],
        "user_prompt": prompt,
        "message_source": "starting_prompt",
    }
    receipt = task_context.evidence(root, "approve-plan", payload)
    assert task_context.evidence(root, "approve-plan", payload) == receipt
    value = task_context.read(root)
    assert value["starting_prompt"] == before["starting_prompt"]
    assert value["corrections"] == before["corrections"]
    assert value["plans"][0]["approval_index"] == 0
    assert value["plans"][0]["starting_prompt_approval"] is True
    gate(root)
    body = task_context.attachment(root)
    assert "starting_prompt_approval binds the immutable starting prompt" in body
    public = proto.parse(body)
    assert public["plans"][0]["starting_prompt_approval"] is True
    assert "private-value" not in body
    message = "Fix exports" + task_context.commit_trailers(root)
    proto.verify_commit(public, message)
    public["plans"][0]["starting_prompt_approval"] = False
    with pytest.raises(ValueError, match="signed head"):
        proto.verify_commit(public, message)
    public["plans"][0].pop("starting_prompt_approval")
    with pytest.raises(ValueError, match="signed head"):
        proto.verify_commit(public, message)
    assert proto.legacy(value) == {**original, "corrections": before["corrections"]}


@pytest.mark.parametrize("gate", [task_context.preflight, task_context.review_record])
def test_starting_prompt_missing_plan_accepts_actual_later_replacement(
    root: Path, gate: Callable[[Path], object]
) -> None:
    task_context.record(root, "Implement the plan.")
    replacement = "Use export fidelity as the replacement scope; the original plan is missing."
    task_context.record(root, replacement, append=True)
    before = task_context.read(root)
    with pytest.raises(ValueError, match="exact approved plan"):
        gate(root)
    task_context.evidence(
        root,
        "replace-missing",
        {"reference": "Implement the plan", "user_prompt": replacement, "correction_index": 0},
    )
    value = task_context.read(root)
    assert value["starting_prompt"] == before["starting_prompt"]
    assert value["corrections"] == [replacement]
    assert value["replacements"] == [{"reference": "Implement the plan", "user_index": 0}]
    gate(root)


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"user_prompt": "Different approval"}, "immutable starting prompt"),
        ({"correction_index": 0}, "cannot include correction_index"),
        ({"correction_index": None}, "cannot include correction_index"),
        ({"message_source": "unknown"}, "Only plan approvals"),
        ({"plan_id": "missing"}, "existing exact evidence ID"),
        ({"unexpected": True}, "exact plan ID"),
    ],
)
def test_starting_prompt_approval_rejects_mismatched_payload_without_mutation(
    root: Path, override: dict[str, Any], error: str
) -> None:
    task_context.record(root, "Implement the plan.")
    plan = task_context.evidence(root, "plan", {"text": "Preserve exports."})
    before = task_context.read(root)
    payload = {
        "plan_id": plan["plan_id"],
        "user_prompt": before["starting_prompt"],
        "message_source": "starting_prompt",
    }
    with pytest.raises(ValueError, match=error):
        task_context.evidence(root, "approve-plan", {**payload, **override})
    assert task_context.read(root) == before


@pytest.mark.parametrize("field", ["starting_prompt_approval", "approval_starting_prompt"])
@pytest.mark.parametrize("flag", [None, 0, 1, "true", [], {}])
def test_starting_prompt_binding_requires_boolean(root: Path, field: str, flag: Any) -> None:
    task_context.record(root, "Feature")
    task_context.evidence(root, "plan", {"text": "Preserve exports."})
    value = task_context.read(root)
    proto = task_context.protocol()
    before = proto.canonical(value)
    value["plans"][0][field] = flag
    with pytest.raises(ValueError, match="starting-prompt plan approval"):
        proto.validate(value)
    value["plans"][0].pop(field)
    assert proto.canonical(value) == before


def test_starting_prompt_binding_preserves_independent_correction_approvals(root: Path) -> None:
    prompt = "Implement the plan."
    task_context.record(root, prompt)
    task_context.record(root, prompt, append=True)
    plan = task_context.evidence(root, "plan", {"text": "Preserve exports."})
    task_context.evidence(
        root,
        "approve-plan",
        {"plan_id": plan["plan_id"], "user_prompt": prompt, "message_source": "starting_prompt"},
    )
    with pytest.raises(ValueError, match="exact approved plan"):
        task_context.preflight(root)
    task_context.evidence(
        root,
        "approve-plan",
        {"plan_id": plan["plan_id"], "user_prompt": prompt, "correction_index": 0},
    )
    task_context.record(root, "Implement the plan, retaining exports.", append=True)
    with pytest.raises(ValueError, match="exact approved plan"):
        task_context.preflight(root)
    task_context.evidence(
        root,
        "approve-plan",
        {
            "plan_id": plan["plan_id"],
            "user_prompt": "Implement the plan, retaining exports.",
            "correction_index": 1,
        },
    )
    task_context.preflight(root)
    value = task_context.read(root)
    assert value["corrections"] == [prompt, "Implement the plan, retaining exports."]
    assert value["plans"][0]["starting_prompt_approval"] is True
    assert value["plans"][0]["approval_index"] == 0
    assert value["plans"][0]["approval_indices"] == [1]


def test_earlier_starting_prompt_binding_retains_signed_v2_bytes(root: Path) -> None:
    task_context.record(root, "Implement the plan.")
    task_context.evidence(root, "plan", {"text": "Preserve exports."})
    value = task_context.read(root)
    proto = task_context.protocol()
    plan = value["plans"][0]
    plan["approval_starting_prompt"] = True
    signed = proto.trailers(value)
    encoded = proto.canonical(value)
    assert proto.parse(proto.render(value)) == value
    proto.require_resolved(value)
    proto.verify_commit(value, signed)
    assert proto.canonical(value) == encoded
    assert "starting_prompt_approval" not in plan
    task_context.save(root, value)
    task_context.evidence(
        root,
        "approve-plan",
        {
            "plan_id": plan["id"],
            "user_prompt": value["starting_prompt"],
            "message_source": "starting_prompt",
        },
    )
    updated = task_context.read(root)["plans"][0]
    assert updated["starting_prompt_approval"] is True
    assert updated["approval_starting_prompt"] is True
    plan["starting_prompt_approval"] = False
    with pytest.raises(ValueError, match="Conflicting starting-prompt"):
        proto.validate(value)
    plan["starting_prompt_approval"] = True
    proto.require_resolved(value)
    with pytest.raises(ValueError, match="signed head"):
        proto.verify_commit(value, signed)


def test_mcp_starting_prompt_approval_gates_handoff_without_duplicate_message(root: Path) -> None:
    prompt = "Implement the plan."
    task_context.record(root, prompt)
    plan = mcp_server.task_context("plan", root=str(root), evidence={"text": "Preserve exports."})
    assert plan["ok"]
    blocked = mcp_server.task_context("review-record", root=str(root))
    assert not blocked["ok"]
    payload = {
        "plan_id": plan["result"]["plan_id"],
        "message_source": "starting_prompt",
        "user_prompt": prompt,
    }
    mismatch = mcp_server.task_context(
        "approve-plan", root=str(root), evidence={**payload, "user_prompt": "Different approval"}
    )
    assert not mismatch["ok"]
    conflict = mcp_server.task_context(
        "approve-plan", root=str(root), evidence={**payload, "correction_index": 0}
    )
    assert not conflict["ok"]
    approval = mcp_server.task_context("approve-plan", root=str(root), evidence=payload)
    assert approval["ok"]
    assert approval["result"]["correction_count"] == 0
    assert mcp_server.task_context("review-record", root=str(root))["ok"]
    assert task_context.read(root)["starting_prompt"] == prompt
    assert task_context.read(root)["corrections"] == []
