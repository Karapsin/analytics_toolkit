from __future__ import annotations

import json
import subprocess
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
