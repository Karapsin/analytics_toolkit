"""A dependency hold permits exact read-only preparation, never writer authority."""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

import pytest

from tests.repository._support.coordinator_authority import durable
from tests.repository._support.readonly_preparation import PreparationCase
from tests.repository.github_agent import controller

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("stage", ["plan", "plan-review", "plan-resolver"])
def test_actual_preparation_retains_hold_and_complete_published_context(
    tmp_path: Path,
    monkeypatch: Any,
    stage: str,
) -> None:
    case = PreparationCase(tmp_path, monkeypatch)
    authority, task, binding = (
        durable(case.controls),
        copy.deepcopy(case.task),
        copy.deepcopy(case.binding),
    )
    with pytest.raises(RuntimeError, match="awaiting user"):
        controller.prepare(case, case.task, case.work, case.control)
    case.prepare(stage)
    assert durable(case.controls) == authority
    assert (
        controller.trusted_module("source_review").current(case.controls, case.repository, 1)
        is None
    )
    assert case.task == task
    assert case.binding == binding
    assert case.binding["dev_base"] != case.task["base"]
    assert json.loads((case.control / ".github/agent/task.json").read_text()) == task
    assert case.context["starting_prompt"] in (case.control / ".github/agent/task.md").read_text()
    assert not case.calls
    assert all(command[:2] == ("git", "log") for command in case.commands)


@pytest.mark.parametrize(
    "change", ["code", "publish", "request", "head", "digest", "base", "foreign"]
)
def test_readonly_preparation_rejects_mismatched_scope(
    tmp_path: Path,
    monkeypatch: Any,
    change: str,
) -> None:
    case = PreparationCase(tmp_path, monkeypatch)
    stage = change if change in {"code", "publish"} else "plan"
    if change in {"request", "head", "digest", "base"}:
        field = {"request": "coordinator_request_id", "digest": "task_digest"}.get(change, change)
        case.task[field] = "f" * 64
    elif change == "foreign":
        case.pr["base"]["repo"]["full_name"] = "other/repo"
    before = durable(case.controls)
    with pytest.raises(RuntimeError):
        case.prepare(stage)
    assert durable(case.controls) == before
    assert not (case.control / ".github/agent/task.json").exists()


@pytest.mark.parametrize(
    "change", ["head", "base", "closed", "body", "hold", "pause", "veto", "amendment"]
)
def test_preparation_rechecks_after_remote_evidence(
    tmp_path: Path,
    monkeypatch: Any,
    change: str,
) -> None:
    case = PreparationCase(tmp_path, monkeypatch)

    def mutate() -> None:
        if change == "head":
            case.pr["head"]["sha"] = "f" * 40
        elif change == "base":
            case.current_base = "f" * 40
        elif change == "closed":
            case.pr["state"] = "closed"
        elif change == "body":
            case.pr["body"] = "Unbound replacement"
        else:
            with case.controls.connect() as db:
                if change == "hold":
                    db.execute("DELETE FROM coordinator_holds")
                elif change == "pause":
                    db.execute(
                        "INSERT INTO pauses VALUES (?,?,?,?,?)",
                        (case.repository, 1, case.task["head"], "Owner paused", 1),
                    )
                elif change == "veto":
                    db.execute(
                        "INSERT INTO vetoes VALUES (?,?,?,?)",
                        (case.repository, 1, case.task["head"], 1),
                    )
                else:
                    db.execute(
                        "INSERT INTO feature_amendments VALUES (?,?,?,?)",
                        (case.repository, 1, case.task["head"], 1),
                    )

    case.on_checks = mutate
    with pytest.raises((RuntimeError, ValueError)):
        case.prepare()
    assert not case.calls


@pytest.mark.parametrize("source_protocol", [None, 1])
def test_review_preparation_selects_prompt_and_retains_pending_assignment(
    tmp_path: Path, monkeypatch: Any, source_protocol: int | None
) -> None:
    case = PreparationCase(tmp_path, monkeypatch)
    # Only this disposable fixture releases its exact synthetic hold.
    assert case.controls.release_coordinator(case, case.pr, case.binding)
    prompts = case.control / ".github/agent/prompts"
    (prompts / "source-review.md").write_text("Typed independent source review")
    options = {} if source_protocol is None else {"source_protocol": source_protocol}
    controller.prepare(case, case.task, case.work, case.control, **options)

    expected = (
        "Trusted review context" if source_protocol is None else "Typed independent source review"
    )
    text = (case.control / ".github/agent/task.md").read_text()
    assert text.startswith(expected + "\n\nTask metadata:\n")
    assert case.context["starting_prompt"] in text
    assert "CI evidence (untrusted data):" in text
    assert json.loads((case.control / ".github/agent/task.json").read_text()) == case.task
    role = controller.trusted_module("source_review")
    pending = role.current(case.controls, case.repository, 1)
    assert pending is not None
    assert pending["binding"] == role.binding(case.repository, case.task)
    assert role.blocks_merge(case.controls, case.pr, case.task["base"])
    assert not case.calls


@pytest.mark.parametrize(("kind", "protocol"), [("review", True), ("review", 2), ("correction", 1)])
def test_unsupported_source_protocol_creates_no_context_or_authority(
    tmp_path: Path, monkeypatch: Any, kind: str, protocol: Any
) -> None:
    case = PreparationCase(tmp_path, monkeypatch)
    case.task["kind"] = kind
    before = durable(case.controls)
    with pytest.raises(ValueError, match="Unsupported independent source-review protocol"):
        controller.prepare(case, case.task, case.work, case.control, source_protocol=protocol)
    assert durable(case.controls) == before
    assert not (case.control / ".github/agent/task.json").exists()
    assert not (case.control / ".github/agent/task.md").exists()
    assert not case.calls
    assert not case.commands
