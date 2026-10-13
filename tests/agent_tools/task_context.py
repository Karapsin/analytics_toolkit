from __future__ import annotations

import base64
import hashlib
import json
import struct
import subprocess
import zlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

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
