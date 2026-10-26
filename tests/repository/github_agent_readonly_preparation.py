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
    with pytest.raises(ValueError, match="Pi source review is retired"):
        controller.prepare(case, case.task, case.work, case.control)
    case.prepare(stage)
    assert durable(case.controls) == authority
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


def test_fraud_review_preparation_writes_prompt_and_bounded_pi_diff(
    tmp_path: Path, monkeypatch: Any
) -> None:
    case = PreparationCase(tmp_path, monkeypatch)
    # Only this disposable fixture releases its exact synthetic hold.
    assert case.controls.release_coordinator(case, case.pr, case.binding)
    prompts = case.control / ".github/agent/prompts"
    (prompts / "fraud-review.md").write_text("Narrow fraud check")
    case.task.update(kind="fraud-review", anchor="c" * 40, fix_commits=["a" * 40])
    diffs = []

    def command(*args: Any, **kwargs: Any) -> str:
        if args[:3] == ("git", "log", "--first-parent"):
            diffs.append(args)
            return "diff --git a/tests/check.py b/tests/check.py\n-    assert result\n"
        return case.command(*args, **kwargs)

    monkeypatch.setattr(controller, "command", command)
    controller.prepare(case, case.task, case.work, case.control)
    text = (case.control / ".github/agent/task.md").read_text()
    assert text.startswith("Narrow fraud check\n\nTask metadata:\n")
    assert '"fix_commits"' in text
    patch = (case.control / ".github/agent/fraud-diff.patch").read_text()
    assert "-    assert result" in patch
    assert diffs[0][-1] == "c" * 40 + ".." + "a" * 40
    assert "--remerge-diff" in diffs[0]
    assert case.controls.correction_snapshot(case.repository, 1) is None
    assert not case.calls


@pytest.mark.parametrize(("kind", "protocol"), [("review", None), ("review", 1), ("correction", 1)])
def test_retired_source_review_creates_no_context_or_authority(
    tmp_path: Path, monkeypatch: Any, kind: str, protocol: Any
) -> None:
    case = PreparationCase(tmp_path, monkeypatch)
    case.task["kind"] = kind
    before = durable(case.controls)
    options = {} if protocol is None else {"source_protocol": protocol}
    with pytest.raises(ValueError, match="Pi source review is retired"):
        controller.prepare(case, case.task, case.work, case.control, **options)
    assert durable(case.controls) == before
    assert not (case.control / ".github/agent/task.json").exists()
    assert not (case.control / ".github/agent/task.md").exists()
    assert not case.calls
    assert not case.commands
